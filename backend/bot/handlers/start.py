"""
SmAttaker — Start & Help Commands
The first thing users see when they launch the bot.
"""
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User, UserStatus
from backend.bot.keyboards.main_menu import get_main_menu_keyboard, get_welcome_keyboard


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — Welcome & authentication."""
    user = update.effective_user
    if not user:
        return

    telegram_id = user.id
    username = user.username
    first_name = user.first_name

    # Check if user exists
    async with async_session_factory() as db:
        result = await db.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        db_user = result.scalar_one_or_none()

        if db_user and db_user.status not in (UserStatus.PENDING_APPROVAL, UserStatus.INACTIVE):
            # Returning active user — show main menu
            await update.message.reply_text(
                f"🦅 *Welcome back, {db_user.full_name or first_name}!*\n\n"
                f"Your SmAttaker dashboard is ready.\n"
                f"_Status: {db_user.status.upper()}_",
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(db_user.language, db_user.role),
            )
        elif db_user and db_user.status == UserStatus.PENDING_APPROVAL:
            await update.message.reply_text(
                "⏳ *Your account is pending admin approval.*\n\n"
                "You'll be notified once approved.\n"
                "Use /subscribe to request a free trial or paid subscription.",
                parse_mode="Markdown",
            )
        else:
            # New user — show welcome
            welcome_text = (
                "🦅 *Welcome to SmAttaker!*\n\n"
                "The *ultimate trading signal system* — powered by AI/ML.\n\n"
                "📊 *Crypto* | 🥇 *Gold* | 💱 *Forex* | 📈 *Stocks*\n\n"
                "🔐 This system is *exclusive & subscription-based*.\n"
                "Get started below 👇"
            )
            await update.message.reply_text(
                welcome_text,
                parse_mode="Markdown",
                reply_markup=get_welcome_keyboard(),
            )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help."""
    help_text = (
        "🦅 *SmAttaker Help Center*\n\n"
        "🔑 */login* — Get a one-tap link to your web dashboard\n"
        "📊 */portfolio* — Your portfolio (Demo/Real)\n"
        "📡 */signals* — Active trading signals\n"
        "📓 */trades* — Trading journal\n"
        "📈 */analytics* — Performance analytics\n"
        "⚠️ */risk* — Risk management settings\n"
        "⚙️ */settings* — Account settings\n"
        "💳 */subscribe* — Subscription plans\n"
        "🌐 */language* — Switch EN/عربي\n"
        "📋 */menu* — Show the main menu\n\n"
        "_Need help? Contact admin: @SmAttakerSupport_"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle any text message that isn't a command."""
    await update.message.reply_text(
        "Use /menu to navigate, /login for the web dashboard, or /help for the full list. 🦅"
    )
