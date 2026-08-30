"""
SmAttaker — Engagement Handler (/progress)
=============================================
Shows the user their own discipline streak and earned badges, and lets
them control digest frequency. Deliberately the only "gamification"
surface in the bot — see backend/services/engagement.py for why it's
built around risk discipline rather than P&L or trade volume.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User
from backend.models.user_engagement import DigestFrequency
from backend.services.engagement import (
    get_or_create_engagement, BADGES, build_digest, format_digest_text,
)
from backend.bot.handlers.menu import register_callback
from backend.bot.utils.safe_edit import safe_edit_message


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /progress."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if db_user:
            await show_progress_menu(update, context, db_user)
        else:
            await update.message.reply_text("Please /start first.")


async def show_progress_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: User):
    query = update.callback_query
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    async with async_session_factory() as db:
        # re-attach a fresh instance in this session
        result = await db.execute(select(User).where(User.id == db_user.id))
        fresh_user = result.scalar_one()
        eng = await get_or_create_engagement(db, fresh_user)
        await db.commit()

        earned_codes = {b["code"] for b in (eng.badges or [])}
        streak = eng.discipline_streak_days
        best = eng.discipline_streak_best

        fire = "🔥" * min(max(streak // 7, 1), 5) if streak > 0 else "—"

        lines = [
            f"🔥 *{t('Your Discipline Streak', 'سلسلة انضباطك')}*",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"{fire}",
            f"*{streak}* {t('days without exceeding your own risk limit', 'يوم بدون تجاوز حد المخاطرة الخاص بك')}",
            f"_{t('Best streak', 'أفضل سلسلة')}: {best} {t('days', 'يوم')}_",
            "",
            f"🎖 *{t('Badges Earned', 'الشارات المكتسبة')}* ({len(earned_codes)}/{len(BADGES)})",
        ]
        for code, badge in BADGES.items():
            name = badge["name_ar"] if is_ar else badge["name_en"]
            if code in earned_codes:
                lines.append(f"{badge['emoji']} {name} ✅")
            else:
                lines.append(f"⬜ {name} — {t('locked', 'مقفلة')}")

        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        digest_label = {
            DigestFrequency.OFF: t("Off", "متوقف"),
            DigestFrequency.WEEKLY: t("Weekly", "أسبوعي"),
            DigestFrequency.MONTHLY: t("Monthly", "شهري"),
        }.get(eng.digest_frequency, eng.digest_frequency)
        lines.append(f"📬 {t('Digest', 'الملخص الدوري')}: *{digest_label}*")

        text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(t("📬 View Digest Now", "📬 عرض الملخص الآن"), callback_data="progress:digest")],
        [InlineKeyboardButton(t("⚙️ Digest Frequency", "⚙️ تكرار الملخص"), callback_data="progress:freq")],
        [InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main")],
    ])

    if query:
        await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("progress")
async def progress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if not db_user:
            return
        is_ar = db_user.language == "ar"
        t = lambda en, ar: ar if is_ar else en

        if payload == "digest":
            digest = await build_digest(db, db_user, days=7)
            text = format_digest_text(db_user, digest)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:progress")
            ]])
            await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)

        elif payload == "freq":
            text = t(
                "📬 *Digest Frequency*\n\nHow often should we send you a self-comparison performance summary?",
                "📬 *تكرار الملخص*\n\nكم مرة تحب أن نرسل لك ملخص أداء مقارَن بنفسك؟",
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(t("📅 Weekly", "📅 أسبوعي"), callback_data="progress:freq:weekly")],
                [InlineKeyboardButton(t("🗓 Monthly", "🗓 شهري"), callback_data="progress:freq:monthly")],
                [InlineKeyboardButton(t("🔕 Off", "🔕 إيقاف"), callback_data="progress:freq:off")],
                [InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:progress")],
            ])
            await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)

        elif payload.startswith("freq:"):
            new_freq = payload.split(":")[1]
            if new_freq in (DigestFrequency.OFF, DigestFrequency.WEEKLY, DigestFrequency.MONTHLY):
                eng = await get_or_create_engagement(db, db_user)
                eng.digest_frequency = new_freq
                await db.commit()
            await show_progress_menu(update, context, db_user)
