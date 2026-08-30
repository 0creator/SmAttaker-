"""
SmAttaker — Web Login Command Handler
Issues a JWT directly from the Telegram bot so the user can open the
web dashboard with a single tap, without relying on the Telegram Login
Widget (which requires the serving domain to be registered with
BotFather via /setdomain — a manual step that's easy to miss and, when
missed, leaves the dashboard completely unreachable with no error).

Why this works:
  - The bot already verified this Telegram identity via Telegram's own
    servers (the bot only ever talks to Telegram, which authenticates
    every update). So mints a JWT here are just as trustworthy as one
    minted after a Telegram Login Widget round-trip.
  - The token is passed as a ?token= query parameter on the dashboard
    URL. The /dashboard route reads it and injects it into the page's
    JavaScript so the dashboard bootstraps with a valid session — the
    user never has to copy/paste anything.
  - The token is a real access JWT (7-day expiry), so "Keep me signed
    in" works the same way it does for the widget login.
"""
import logging

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User, UserRole, UserStatus
from backend.config import settings
from backend.utils.security import create_access_token, create_refresh_token

logger = logging.getLogger("smattaker.bot.weblogin")


async def _issue_weblogin_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    db_user: User,
) -> None:
    """Mint a JWT for an already-loaded user and send a dashboard link.

    Shared by the /login command handler (which loads the user itself)
    and the main-menu "Get Dashboard Link" callback (which already has
    the user from the menu router). Both paths end up here so the token
    minting logic lives in exactly one place.
    """
    from sqlalchemy import select

    # Refresh the user's refresh-jti so the new token pair is the only
    # valid one. We re-load the user inside a fresh session so the
    # commit persists the new jti.
    async with async_session_factory() as db:
        result = await db.execute(
            select(User).where(User.id == db_user.id)
        )
        fresh_user = result.scalar_one_or_none()
        if not fresh_user:
            logger.error(
                f"_issue_weblogin_link: user {db_user.id} disappeared "
                f"before token mint — aborting."
            )
            return

        access_token = create_access_token(
            str(fresh_user.id), fresh_user.telegram_id
        )
        refresh_token, jti = create_refresh_token(
            str(fresh_user.id), fresh_user.telegram_id
        )
        fresh_user.current_refresh_jti = jti
        await db.commit()

        is_admin = fresh_user.role == UserRole.ADMIN
        status_label = (
            fresh_user.status.upper() if fresh_user.status else "UNKNOWN"
        )

        base_url = settings.RENDER_EXTERNAL_URL.rstrip("/")
        dashboard_url = f"{base_url}/dashboard?token={access_token}#rt={refresh_token}"

        # Build the keyboard: primary dashboard button + (for admins) an
        # admin panel button. A url= button opens directly in the
        # device's browser — no callback round-trip, no copy/paste.
        buttons = [[InlineKeyboardButton(
            "🌐 Open My Dashboard",
            url=dashboard_url,
        )]]
        if is_admin:
            admin_url = f"{base_url}/admin?token={access_token}"
            buttons.append([InlineKeyboardButton(
                "🔐 Open Admin Panel",
                url=admin_url,
            )])
        keyboard = InlineKeyboardMarkup(buttons)

        # Reply on the original message (works for both command and
        # callback contexts — the command path uses update.message,
        # the callback path uses update.callback_query.message).
        reply_target = update.message or (
            update.callback_query.message if update.callback_query else None
        )
        if reply_target is None:
            logger.warning("No message target to send the web login link to.")
            return

        await reply_target.reply_text(
            f"🔑 *Web Dashboard Access*\n\n"
            f"Status: `{status_label}`\n"
            f"Token valid for 7 days.\n\n"
            f"Tap the button below to open your dashboard. "
            f"You'll stay signed in on this device.\n\n"
            f"_Send /login anytime for a fresh link._",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

        logger.info(
            f"Issued web login token for user {fresh_user.id} "
            f"(tg={fresh_user.telegram_id}, role={fresh_user.role})"
        )


async def weblogin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /login — issue a JWT for the current Telegram user and send a
    one-tap link to the web dashboard.

    Available to every user (not just admins). This is the primary,
    reliable way to reach the web dashboard: the Telegram Login Widget
    on /login (the web page) requires the serving domain to be
    registered with @BotFather via /setdomain, which is a manual step
    that's easy to forget. This bot command bypasses that entirely.
    """
    user = update.effective_user
    if not user:
        return

    try:
        await context.bot.send_chat_action(
            chat_id=user.id, action=ChatAction.TYPING
        )
    except Exception:
        pass  # non-fatal — the typing indicator is cosmetic

    async with async_session_factory() as db:
        result = await db.execute(
            select(User).where(User.telegram_id == user.id)
        )
        db_user = result.scalar_one_or_none()

        if not db_user:
            # First contact — tell them to /start to register.
            await update.message.reply_text(
                "👋 You're not registered yet.\n\n"
                "Send /start to create your SmAttaker account first, "
                "then /login to open the web dashboard.",
            )
            return

        if db_user.is_banned:
            await update.message.reply_text("⛔ Your account has been banned.")
            return

        await _issue_weblogin_link(update, context, db_user)
