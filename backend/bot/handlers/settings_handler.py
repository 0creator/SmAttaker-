"""
SmAttaker — Settings Handler
User account settings: language, profile, account type, exchange connections.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User


from backend.bot.handlers.menu import register_callback
from backend.bot.utils.safe_edit import safe_edit_message


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()
        if db_user:
            await show_settings_menu(update, context, db_user)
        else:
            await update.message.reply_text("Please /start first.")


async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user: User):
    """Display user settings — includes profile info (email, telegram, etc.)."""
    query = update.callback_query
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    # Account type label
    acc_type = db_user.default_account_type or "paper"
    acc_labels = {
        "paper": ("📝 PAPER", "📝 ورقي"),
        "demo": ("🧪 DEMO", "🧪 تجريبي"),
        "real": ("💰 REAL", "💰 حقيقي"),
    }
    acc_label = acc_labels.get(acc_type, acc_labels["paper"])[1 if is_ar else 0]

    # Paper balance display (only for paper)
    balance_line = ""
    if acc_type == "paper":
        paper_bal = float(db_user.paper_balance or 10000.0)
        paper_init = float(db_user.paper_initial_balance or 10000.0)
        pnl = paper_bal - paper_init
        pnl_pct = (pnl / paper_init * 100) if paper_init > 0 else 0
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        balance_line = (
            f"💵 {t('Paper Balance', 'الرصيد الوهمي')}: *${paper_bal:,.2f}* "
            f"({pnl_emoji} {pnl:+.2f} / {pnl_pct:+.2f}%)\n"
        )

    text = (
        f"⚙️ *{t('Settings', 'الإعدادات')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 *{t('Profile', 'الملف الشخصي')}*\n"
        f"   {t('Name', 'الاسم')}: *{db_user.full_name or 'N/A'}*\n"
        f"   📧 {t('Email', 'البريد')}: *{db_user.email or 'N/A'}*\n"
        f"   💬 {t('Telegram', 'تيليجرام')}: @{db_user.telegram_username or 'N/A'}\n"
        f"   🆔 {t('Telegram ID', 'معرف تيليجرام')}: `{db_user.telegram_id}`\n"
        f"   🌐 {t('Language', 'اللغة')}: *{db_user.language.upper()}*\n"
        f"   📊 {t('Account Type', 'نوع الحساب')}: *{acc_label}*\n"
        f"{balance_line}"
        f"   📅 {t('Member Since', 'عضو منذ')}: *{db_user.created_at.strftime('%Y-%m-%d') if db_user.created_at else 'N/A'}*\n"
        f"   🔄 {t('Status', 'الحالة')}: *{db_user.status.upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🌐 {t('Switch to العربية', 'Switch to English')}",
            callback_data="settings:lang:toggle"
        )],
        [InlineKeyboardButton(
            t("📊 Change Account Type", "📊 تغيير نوع الحساب"), callback_data="settings:account:choose"
        )],
        [InlineKeyboardButton(
            t("📧 Update Email", "📧 تحديث البريد"), callback_data="settings:email"
        )],
        [InlineKeyboardButton(
            t("🔗 Exchange Connections", "🔗 ربط المنصات"), callback_data="settings:exchanges"
        )],
        [InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:main")],
    ])

    if query:
        await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


@register_callback("settings")
async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
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

        if payload == "lang:toggle":
            db_user.language = "ar" if db_user.language == "en" else "en"
            await db.commit()
            await show_settings_menu(update, context, db_user)
        elif payload == "account:choose":
            # Show account type chooser
            acc_type = db_user.default_account_type or "paper"
            text = (
                f"📊 *{t('Choose Account Type', 'اختر نوع الحساب')}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📝 *{t('PAPER', 'ورقي')}* — {t('Virtual $10,000 (default)', 'وهمي 10,000$ (افتراضي)')}\n"
                f"🧪 *{t('DEMO', 'تجريبي')}* — {t('Exchange/MT5 testnet', 'منصة/MT5 تجريبي')}\n"
                f"💰 *{t('REAL', 'حقيقي')}* — {t('Real money', 'أموال حقيقية')}\n\n"
                f"_{t('Current', 'الحالي')}: {acc_type.upper()}_"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📝 {t('PAPER Trading', 'تداول ورقي')}", callback_data="settings:account:set:paper"
                )],
                [InlineKeyboardButton(
                    f"🧪 {t('DEMO Account', 'حساب تجريبي')}", callback_data="settings:account:set:demo"
                )],
                [InlineKeyboardButton(
                    f"💰 {t('REAL Account', 'حساب حقيقي')}", callback_data="settings:account:set:real"
                )],
                [InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:settings")],
            ])
            await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
        elif payload.startswith("account:set:"):
            new_type = payload.split(":")[2]
            if new_type in ("paper", "demo", "real"):
                db_user.default_account_type = new_type
                if new_type == "paper" and not db_user.paper_balance:
                    db_user.paper_balance = 10000.0
                    db_user.paper_initial_balance = 10000.0
                await db.commit()
                await show_settings_menu(update, context, db_user)
        elif payload == "email":
            is_ar = db_user.language == "ar"
            t = lambda en, ar: ar if is_ar else en
            text = t(
                "📧 To update your email, please use the `/auth` command.",
                "📧 لتحديث بريدك الإلكتروني، يرجى استخدام الأمر `/auth`."
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(t("🔙 Back", "🔙 رجوع"), callback_data="menu:settings")
            ]])
            await safe_edit_message(query, text, parse_mode="Markdown", reply_markup=keyboard)
        elif payload == "exchanges":
            from backend.bot.handlers.portfolio import portfolio_callback
            await portfolio_callback(update, context, "exchanges")
