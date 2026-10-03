"""
SmAttaker — Language Handler
Switch between English and Arabic.
"""
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /language — toggle between EN and AR."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.telegram_id == user.id))
        db_user = result.scalar_one_or_none()

        if not db_user:
            await update.message.reply_text("Please /start first.")
            return

        # Toggle language
        new_lang = "ar" if db_user.language == "en" else "en"
        db_user.language = new_lang
        await db.commit()

        if new_lang == "ar":
            await update.message.reply_text(
                "🌐 *تم تغيير اللغة إلى العربية!*\n\n"
                "استخدم /menu للعودة إلى القائمة الرئيسية.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "🌐 *Language switched to English!*\n\n"
                "Use /menu to return to the main menu.",
                parse_mode="Markdown",
            )
