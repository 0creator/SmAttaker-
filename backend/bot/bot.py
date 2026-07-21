"""
SmAttaker — Telegram Bot Initialization
The main bot instance and all handler registration.
"""
import logging
from telegram import Update, BotCommand
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, filters,
)
from backend.config import settings
from backend.redis_client import get_redis
from backend.bot.keyboards.main_menu import get_main_menu_keyboard

# ── Logger ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("smattaker.bot")

# ── State Constants for Conversation Handlers ───────────
(
    STATE_EMAIL, STATE_WAITING, STATE_RISK_NAME,
    STATE_RISK_VALUE, STATE_EXCHANGE_NAME, STATE_API_KEY,
    STATE_SECRET_KEY, STATE_PASSPHRASE, STATE_NOTES,
) = range(9)

# ── Bot Application ─────────────────────────────────────
bot_app: Application | None = None


async def init_bot() -> Application:
    """Initialize and configure the Telegram bot."""
    global bot_app

    bot_app = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Register all handlers
    _register_handlers(bot_app)

    # Set bot commands (menu)
    await bot_app.bot.set_my_commands([
        BotCommand("start", "🦅 Launch SmAttaker"),
        BotCommand("menu", "📋 Main Menu"),
        BotCommand("portfolio", "📊 Portfolio"),
        BotCommand("signals", "📡 Active Signals"),
        BotCommand("trades", "📓 Trade Journal"),
        BotCommand("analytics", "📈 Analytics"),
        BotCommand("risk", "⚠️ Risk Management"),
        BotCommand("settings", "⚙️ Settings"),
        BotCommand("subscribe", "💳 Subscribe"),
        BotCommand("help", "❓ Help & Support"),
        BotCommand("language", "🌐 EN/عربي"),
        BotCommand("login", "🔑 Open Web Dashboard"),
    ])

    logger.info("🤖 SmAttaker Bot initialized!")
    return bot_app


def _register_handlers(app: Application):
    """Register all command and callback handlers."""
    from backend.bot.handlers import (
        start, auth, menu, portfolio, signals,
        trades, analytics, risk, settings_handler,
        subscription, admin, language, weblogin,
    )

    # ── Command Handlers ────────────────────────────
    app.add_handler(CommandHandler("start", start.start_command))
    app.add_handler(CommandHandler("menu", menu.menu_command))
    app.add_handler(CommandHandler("portfolio", portfolio.portfolio_command))
    app.add_handler(CommandHandler("signals", signals.signals_command))
    app.add_handler(CommandHandler("trades", trades.trades_command))
    app.add_handler(CommandHandler("analytics", analytics.analytics_command))
    app.add_handler(CommandHandler("risk", risk.risk_command))
    app.add_handler(CommandHandler("settings", settings_handler.settings_command))
    app.add_handler(CommandHandler("subscribe", subscription.subscribe_command))
    app.add_handler(CommandHandler("help", start.help_command))
    app.add_handler(CommandHandler("language", language.language_command))
    app.add_handler(CommandHandler("login", weblogin.weblogin_command))
    app.add_handler(CommandHandler("admin", admin.admin_command))
    app.add_handler(CommandHandler("webtoken", admin.webtoken_command))
    app.add_handler(CommandHandler("admin_broadcast", admin.broadcast_command))

    # ── Callback Query Handler (all inline buttons) ──
    app.add_handler(CallbackQueryHandler(menu.callback_router))

    # ── Conversation Handlers ────────────────────────
    app.add_handler(auth.auth_conversation_handler())
    # ⚠️ risk.risk_conversation_handler() removed — it had empty
    # entry_points (unreachable) and no-op state handlers (would have
    # silently discarded any input if it had ever been triggered). Real
    # risk-settings editing lives in the web dashboard's Risk Settings
    # tab (PUT /api/account/risk), which actually persists changes.
    app.add_handler(subscription.subscription_conversation_handler())

    # ── Fallback ─────────────────────────────────────
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start.fallback_handler))

    logger.info("  ✅ All handlers registered")


async def start_bot():
    """Start the bot (called from main.py or standalone)."""
    if bot_app is None:
        await init_bot()
    logger.info("🦅 SmAttaker Bot is RUNNING...")
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)


async def stop_bot():
    """Stop the bot gracefully."""
    global bot_app
    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        logger.info("🦅 SmAttaker Bot stopped.")
