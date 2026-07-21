"""
SmAttaker — Settings Handler
User account settings: language, profile, exchange connections.
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
    """Display user settings."""
    query = update.callback_query
    is_ar = db_user.language == "ar"
    t = lambda en, ar: ar if is_ar else en

    text = (
        f"⚙️ *{t('Settings', 'الإعدادات')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {t('Name', 'الاسم')}: *{db_user.full_name or 'N/A'}*\n"
        f"📧 {t('Email', 'البريد')}: *{db_user.email or 'N/A'}*\n"
        f"🌐 {t('Language', 'اللغة')}: *{db_user.language.upper()}*\n"
        f"📊 {t('Default Account', 'الحساب الافتراضي')}: *{db_user.default_account_type.upper()}*\n"
        f"📅 {t('Member Since', 'عضو منذ')}: *{db_user.created_at.strftime('%Y-%m-%d') if db_user.created_at else 'N/A'}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🌐 {t('Switch to العربية', 'Switch to English')}",
            callback_data="settings:lang:toggle"
        )],
        [InlineKeyboardButton(
            t("📊 Default: Demo", "📊 الافتراضي: تجريبي"), callback_data="settings:account:toggle"
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

        if payload == "lang:toggle":
            db_user.language = "ar" if db_user.language == "en" else "en"
            await db.commit()
            await show_settings_menu(update, context, db_user)
        elif payload == "account:toggle":
            db_user.default_account_type = "real" if db_user.default_account_type == "demo" else "demo"
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
