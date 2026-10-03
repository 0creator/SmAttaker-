"""
SmAttaker — Authentication Conversation Handler
Handles email input for trial registration.
"""
from telegram import Update
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, filters,
)
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User, UserStatus
from backend.models.admin_notification import AdminNotification, NotificationType
from backend.bot.bot import STATE_EMAIL
from backend.bot.utils.safe_edit import safe_edit_message

# ── Conversation: Trial Email Input ─────────────────────
async def trial_email_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start trial — ask for email."""
    query = update.callback_query
    if query:
        await query.answer()
        await safe_edit_message(query, 
            "📧 *Free Trial — Enter Your Gmail*\n\n"
            "Please enter your Gmail address to request a 3-day free trial.\n"
            "_Your request will be reviewed by the admin._",
            parse_mode="Markdown",
        )
    return STATE_EMAIL


async def trial_email_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process email input."""
    email = update.message.text.strip()
    user_id = update.effective_user.id

    if "@" not in email or "." not in email:
        await update.message.reply_text("❌ Invalid email. Please enter a valid Gmail address.")
        return STATE_EMAIL

    async with async_session_factory() as db:
        result = await db.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            # Create user
            user = User(
                telegram_id=user_id,
                telegram_username=update.effective_user.username,
                full_name=update.effective_user.full_name,
                email=email,
                status=UserStatus.PENDING_APPROVAL,
                language="en",
            )
            db.add(user)
            await db.flush()
        else:
            user.email = email
            user.status = UserStatus.PENDING_APPROVAL

        # Notify admin
        notif = AdminNotification(
            notification_type=NotificationType.TRIAL_REQUEST,
            title="New Trial Request",
            message=(
                f"📩 *New Trial Request*\n\n"
                f"User: @{user.telegram_username or user.telegram_id}\n"
                f"Email: {email}\n"
                f"Trial: 3 days free"
            ),
            severity="info",
            related_user_id=user.id,
        )
        db.add(notif)

    await update.message.reply_text(
        "✅ *Trial request submitted!*\n\n"
        "Your request has been sent to the admin for review.\n"
        "You'll receive a notification once approved.\n\n"
        "_This usually takes a few minutes._ 🦅",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def trial_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Trial request cancelled. Use /start to try again.")
    return ConversationHandler.END


def auth_conversation_handler() -> ConversationHandler:
    """Build the auth conversation handler."""
    return ConversationHandler(
        entry_points=[],
        states={
            STATE_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, trial_email_received),
            ],
        },
        fallbacks=[CommandHandler("cancel", trial_cancel)],
    )
