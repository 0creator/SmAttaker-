"""
SmAttaker — Main Menu & Callback Router
Routes all inline button callbacks to the right handler.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select

from backend.database import async_session_factory
from backend.models.user import User, UserStatus, UserRole
from backend.models.trade import Trade, TradeStatus
from backend.models.signal import Signal, SignalStatus
from backend.bot.keyboards.main_menu import get_main_menu_keyboard, get_welcome_keyboard
from backend.bot.utils.safe_edit import safe_edit_message

logger = logging.getLogger("smattaker.bot.menu")

# ── Callback Router ─────────────────────────────────────
CALLBACK_MAP = {}


def register_callback(prefix: str):
    """Decorator to register a callback handler."""
    def decorator(func):
        CALLBACK_MAP[prefix] = func
        return func
    return decorator


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main callback router — dispatches to the right handler.
    All inline button callbacks flow through here.
    """
    query = update.callback_query
    if not query:
        return

    data = query.data
    if not data:
        await query.answer()
        return

    # Extract prefix (everything before first ':')
    parts = data.split(":", 1)
    prefix = parts[0]
    payload = parts[1] if len(parts) > 1 else ""

    handler = CALLBACK_MAP.get(prefix)
    if handler:
        try:
            await handler(update, context, payload)
        except Exception as e:
            logger.error(
                f"Unhandled error in callback handler '{prefix}' "
                f"(payload={payload!r}): {e}",
                exc_info=True,
            )
            # Show the user a non-blocking alert so the button does not
            # look silently dead. The real error is logged above.
            try:
                await query.answer(
                    "Something went wrong on our end. Please try again "
                    "or send /start to reset the menu.",
                    show_alert=True,
                )
            except Exception:
                pass
    else:
        await query.answer(f"Unknown action: {prefix}", show_alert=True)


# ── Menu Command ────────────────────────────────────────
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /menu — show main menu."""
    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(
            select(User).where(User.telegram_id == user.id)
        )
        db_user = result.scalar_one_or_none()
        lang = db_user.language if db_user else "en"
        role = db_user.role if db_user else UserRole.USER

        # Get quick stats for the menu header
        active_trades_count = 0
        active_signals_count = 0
        if db_user:
            tres = await db.execute(
                select(Trade).where(
                    Trade.user_id == db_user.id,
                    Trade.status == TradeStatus.ACTIVE,
                )
            )
            active_trades_count = len(tres.scalars().all())
            # Count active signals available to this user
            sres = await db.execute(
                select(Signal).where(Signal.status == SignalStatus.ACTIVE)
            )
            active_signals_count = len(sres.scalars().all())

        is_ar = (db_user.language if db_user else "en") == "ar"
        t = lambda en, ar: ar if is_ar else en

        header = (
            f"🦅 *S M A T T A K E R*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 {t('Active Signals', 'الإشارات النشطة')}: *{active_signals_count}*  ·  "
            f"📊 {t('Active Trades', 'صفقاتي المفتوحة')}: *{active_trades_count}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"_{t('Navigate your trading empire:', 'تصفح إمبراطوريتك التجارية:')}_"
        )
        await update.message.reply_text(
            header,
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(lang, role),
        )


# ── Navigation Callbacks ────────────────────────────────
@register_callback("menu")
async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    """Handle menu navigation callbacks."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    async with async_session_factory() as db:
        result = await db.execute(
            select(User).where(User.telegram_id == user.id)
        )
        db_user = result.scalar_one_or_none()
        lang = db_user.language if db_user else "en"
        role = db_user.role if db_user else UserRole.USER

    # Delegate to specific section handlers
    section_handlers = {
        "portfolio": show_portfolio,
        "risk": show_risk,
        "journal": show_journal,
        "analytics": show_analytics,
        "settings": show_settings,
        "subscribe": show_subscription,
        "admin": show_admin_panel,
        "weblogin": show_weblogin,
    }

    if payload in section_handlers:
        await section_handlers[payload](update, context, db_user)
    elif payload == "welcome" or payload == "main" and not db_user:
        welcome_text = (
            "🦅 *Welcome to SmAttaker!*\n\n"
            "The *ultimate trading signal system* — powered by AI/ML.\n\n"
            "📊 *Crypto* | 🥇 *Gold* | 💱 *Forex* | 📈 *Stocks*\n\n"
            "🔐 This system is *exclusive & subscription-based*.\n"
            "Get started below 👇"
        )
        await safe_edit_message(query, 
            welcome_text,
            parse_mode="Markdown",
            reply_markup=get_welcome_keyboard(),
        )
    else:
        await safe_edit_message(query, 
            "🦅 *Main Menu*\nSelect a section to continue.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(lang, role),
        )


# ── Section Placeholders (detailed implementations below) ─
async def show_portfolio(update, context, db_user):
    from backend.bot.handlers.portfolio import show_portfolio_menu
    await show_portfolio_menu(update, context, db_user)


async def show_risk(update, context, db_user):
    from backend.bot.handlers.risk import show_risk_menu
    await show_risk_menu(update, context, db_user)


async def show_journal(update, context, db_user):
    from backend.bot.handlers.trades import show_journal_menu
    await show_journal_menu(update, context, db_user)


async def show_analytics(update, context, db_user):
    from backend.bot.handlers.analytics import show_analytics_menu
    await show_analytics_menu(update, context, db_user)


async def show_settings(update, context, db_user):
    from backend.bot.handlers.settings_handler import show_settings_menu
    await show_settings_menu(update, context, db_user)


async def show_subscription(update, context, db_user):
    from backend.bot.handlers.subscription import show_subscription_menu
    await show_subscription_menu(update, context, db_user)


async def show_admin_panel(update, context, db_user):
    from backend.bot.handlers.admin import show_admin_menu
    await show_admin_menu(update, context, db_user)


async def show_weblogin(update, context, db_user):
    """Mint a JWT for the current user and send a one-tap dashboard link.

    Triggered by the 'Get Dashboard Link' button in the main menu (and
    the menu:weblogin callback). This bypasses the Telegram Login Widget
    entirely — the bot already verified the Telegram identity, so it can
    mint a JWT directly and hand the user a link that logs them in with
    a single tap.
    """
    from backend.bot.handlers.weblogin import _issue_weblogin_link
    await _issue_weblogin_link(update, context, db_user)
