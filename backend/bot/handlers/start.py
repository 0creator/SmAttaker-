"""
SmAttaker — Start & Help Commands
The first thing users see when they launch the bot.

Onboarding flow:
  1. Brand-new user (no record) → show welcome + onboarding prompt
  2. Existing user without onboarding_completed → show onboarding prompt
  3. Returning user with onboarding_completed → show main menu
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
            # Returning user — check onboarding
            if not db_user.onboarding_completed:
                # Existing user who hasn't completed onboarding → prompt
                from backend.bot.handlers.onboarding import show_onboarding
                await show_onboarding(update, context, db_user)
                return

            # Fully onboarded returning user — show main menu
            await update.message.reply_text(
                f"🦅 *Welcome back, {db_user.full_name or first_name}!*\n\n"
                f"Your SmAttaker dashboard is ready.\n"
                f"_Status: {db_user.status.upper()}_",
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(db_user.language, db_user.role),
            )
        elif db_user and db_user.status == UserStatus.PENDING_APPROVAL:
            # User pending admin approval (trial request submitted)
            await update.message.reply_text(
                "⏳ *Your account is pending admin approval.*\n\n"
                "You'll be notified once approved.\n"
                "Use /subscribe to request a free trial or paid subscription.",
                parse_mode="Markdown",
            )
        else:
            # Brand-new user — show welcome + onboarding
            # Create the user record first so onboarding can update it
            new_user = User(
                telegram_id=telegram_id,
                telegram_username=username,
                full_name=first_name,
                status=UserStatus.PENDING_APPROVAL,
                language="en",
                default_account_type="paper",
                onboarding_completed=False,
                paper_balance=10000.0,
                paper_initial_balance=10000.0,
            )
            db.add(new_user)
            await db.commit()
            await db.refresh(new_user)

            from backend.bot.handlers.onboarding import show_onboarding
            await show_onboarding(update, context, new_user)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help."""
    help_text = (
        "🦅 *SmAttaker Help Center*\n\n"
        "🔑 */login* — Get a one-tap link to your web dashboard\n"
        "📊 */portfolio* — Your portfolio (Paper/Demo/Real)\n"
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
