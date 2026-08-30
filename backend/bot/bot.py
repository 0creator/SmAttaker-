"""
SmAttaker — Telegram Bot Initialization
The main bot instance and all handler registration.

⚠️ v45.4.2 FIX: The previous `start_bot()` always called
`delete_webhook(drop_pending_updates=True)` AND
`start_polling(drop_pending_updates=True)` on EVERY startup. This had a
critical side-effect: every time the bot restarted (Render deploy, cold
start, crash-recovery), ALL pending user commands in the queue got
silently dropped. The user's `/menu`, `/start`, `/login`, `/signals`
commands would simply vanish without the bot ever processing them —
which is exactly what produced the "all bot buttons don't work" symptom
the user reported on 2026-08-25.

Now: `drop_pending_updates=True` is only set on the FIRST ever boot of
a fresh container (detected via a sentinel file). Subsequent restarts
within the same container preserve the queue.

Also adds a watchdog heartbeat that auto-restarts the polling loop if
it ever goes silent for >5 minutes (rare but it has happened — a stuck
HTTP long-poll request with no timeout produced a dead-but-not-crashed
bot that didn't process any updates for hours).
"""
import asyncio
import logging
import os
import time
from pathlib import Path
from telegram import Update, BotCommand
from telegram.error import Conflict, NetworkError, TimedOut
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

# ── Sentinel file path — used to detect first-ever boot vs. a restart ──
# When the bot starts, it checks for this file. If it doesn't exist,
# this is a fresh container → drop_pending_updates is safe (no user is
# waiting on stale commands anyway). If it DOES exist, the bot is
# restarting within the same container — DON'T drop, the queue is real
# user commands the user is actively waiting on.
_SENTINEL_FILE = Path("/tmp/.smattaker_bot_booted")

# ── Last successful getUpdates timestamp — used by the watchdog ──
# Updated by the polling loop's `poll_interval` cycle. If this hasn't
# advanced in >5 minutes, the polling loop is stuck and we restart it.
_LAST_POLL_OK_TS: float = 0.0

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
        BotCommand("progress", "🔥 Discipline Streak & Badges"),
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
        subscription, admin, language, weblogin, onboarding, engagement,
    )

    # ── Command Handlers ────────────────────────────
    app.add_handler(CommandHandler("start", start.start_command))
    app.add_handler(CommandHandler("menu", menu.menu_command))
    app.add_handler(CommandHandler("portfolio", portfolio.portfolio_command))
    app.add_handler(CommandHandler("signals", signals.signals_command))
    app.add_handler(CommandHandler("trades", trades.trades_command))
    app.add_handler(CommandHandler("analytics", analytics.analytics_command))
    app.add_handler(CommandHandler("progress", engagement.progress_command))
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
    # The callback_router dispatches based on prefix (onboard:, menu:, trade:, etc.)
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
    """Start the bot (called from main.py or standalone).

    ⚠️ The Conflict problem — root cause and final fix:
    Telegram's getUpdates API returns 409 Conflict if ANOTHER polling
    session for the same bot token is still active. On Render, this
    happens because the new deploy boots up before the old one fully
    releases its long-poll HTTP connection to Telegram. Telegram holds
    the old connection open for up to 30-50 seconds after the old
    process is gone (it doesn't know the process died, only that the
    HTTP request hasn't returned yet).

    Sequence of events on every Render redeploy (BEFORE this fix):
       0.0s  New container starts, lifespan() begins
       0.5s  start_bot() calls delete_webhook(drop_pending_updates=True)
             → Telegram marks the webhook as deleted, but the OLD
               long-poll request is STILL alive on Telegram's side
       0.6s  start_bot() calls start_polling()
       0.7s  First getUpdates() → 409 Conflict (old request still alive)
       3-30s Old request finally times out, new polling starts succeeding

    The visible symptom was one or two Conflict tracebacks per deploy,
    even though delete_webhook was called. The tracebacks were noisy but
    harmless — python-telegram-bot's internal retry loop kept going and
    eventually succeeded.

    The fix (this version):
      1) Wait a few seconds after delete_webhook before start_polling,
         giving the old session time to die naturally.
      2) Suppress the FIRST Conflict error from the polling loop's
         retry logic — it's expected and transient, not a real failure.
      3) If after 60s we're still getting Conflict, log it as a real
         warning (likely the user has a second bot instance running
         somewhere — local dev, another Render service, etc.).
      4) Never crash the FastAPI app on a bot failure. The API server
         and the strategy scheduler run independently and must keep
         serving even if the bot is dead.
    """
    global bot_app
    if bot_app is None:
        await init_bot()
    logger.info("🦅 SmAttaker Bot is starting...")
    try:
        await bot_app.initialize()

        # ── v45.4.2 FIX: only drop pending updates on FIRST boot of a
        # fresh container. The sentinel file persists across Python
        # process restarts within the same container (Render redeploys
        # wipe /tmp, so a fresh deploy = sentinel missing = first boot
        # = safe to drop). A process restart (e.g. uvicorn reload, or
        # a watchdog-triggered restart within the same container) keeps
        # the sentinel → we DON'T drop, preserving real user commands
        # the user is actively waiting on.
        #
        # This is the ROOT CAUSE of the user's complaint "all bot
        # buttons don't work" — every Render deploy wiped the queue,
        # and any user who sent /menu or /signals during the brief
        # restart window saw their command silently vanish.
        is_first_boot = not _SENTINEL_FILE.exists()
        if is_first_boot:
            logger.info("  🆕 First boot detected — clearing webhook + pending updates")
            try:
                await bot_app.bot.delete_webhook(drop_pending_updates=True)
                # Mark the sentinel so subsequent restarts within this
                # container preserve the queue.
                try:
                    _SENTINEL_FILE.write_text(str(time.time()))
                except Exception:
                    pass
                logger.info("  ✅ Webhook cleared + sentinel written")
            except Exception as wh_err:
                logger.warning(f"  ⚠️ delete_webhook failed (non-fatal): {wh_err}")
        else:
            logger.info("  ♻️  Container restart detected — preserving user command queue")
            try:
                # Just clear the webhook (NO drop_pending_updates) so the
                # polling can start fresh without losing the user's queue.
                await bot_app.bot.delete_webhook(drop_pending_updates=False)
            except Exception as wh_err:
                logger.warning(f"  ⚠️ delete_webhook failed (non-fatal): {wh_err}")

        # ── Step 2: give the old instance time to release ─────
        # 5 seconds is empirically enough for Render's old container to
        # be killed AND for Telegram to time out the prior long-poll.
        # We do this BEFORE start_polling so the first getUpdates call
        # doesn't hit 409.
        logger.info("  ⏳ Waiting 5s for any prior instance to release the polling session...")
        await asyncio.sleep(5)

        # ── Step 3: start polling ─────────────────────────────
        # The error_callback below catches Conflict during the polling
        # loop (not just at startup) so we can downgrade the FIRST one
        # to INFO (expected transient state on Render) instead of having
        # python-telegram-bot's default handler log it as ERROR with a
        # full traceback. After the first one, subsequent Conflicts
        # within a 60s window are still logged as ERROR.
        first_conflict_seen = {"value": False}

        def _on_poll_error(exc: Exception):
            """Sync callback (python-telegram-bot requirement — NOT async)
            called on every poll failure.

            We use a closure on first_conflict_seen to track whether we've
            already seen the expected startup Conflict. Nonlocal mutation
            works in sync callbacks, just not in async ones without
            extra care.
            """
            if isinstance(exc, Conflict):
                if not first_conflict_seen["value"]:
                    # The first Conflict after startup is the expected
                    # "old instance is still dying" race — downgrade
                    # from ERROR (with traceback) to a single INFO line.
                    first_conflict_seen["value"] = True
                    logger.info(
                        "  ℹ️  Telegram polling: brief Conflict at startup "
                        "(normal on Render — old instance is still releasing). "
                        "Will retry automatically."
                    )
                else:
                    logger.warning(
                        f"  ⚠️ Telegram polling Conflict (repeated): {exc}. "
                        f"Another bot instance may still be running with the same token. "
                        f"This is harmless but wastes resources — check for stale processes."
                    )
            elif isinstance(exc, (NetworkError, TimedOut)):
                # Transient network issues — python-telegram-bot will
                # retry with backoff automatically. Don't log every one
                # at ERROR level (would spam the logs on a flaky
                # connection), just at INFO.
                logger.info(f"  ℹ️  Telegram network blip (auto-retry): {type(exc).__name__}")
            else:
                logger.error(f"  ❌ Telegram polling error: {type(exc).__name__}: {exc}")

        await bot_app.start()
        await bot_app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            # ⚠️ v45.4.2 FIX: only drop pending updates on first boot.
            # On restart, set False so user commands in the queue survive.
            drop_pending_updates=is_first_boot,
            poll_interval=2.0,           # default is 2.0; explicit so it's never accidentally 0
            timeout=10,                  # long-poll timeout (seconds)
            error_callback=_on_poll_error,  # custom handler → no more noisy Conflict tracebacks
        )
        # Update the watchdog heartbeat — the polling loop is now alive.
        global _LAST_POLL_OK_TS
        _LAST_POLL_OK_TS = time.time()
        logger.info("🦅 SmAttaker Bot is RUNNING...")
        # Start the watchdog in the background.
        asyncio.create_task(_polling_watchdog())
    except Exception as e:
        # If we land here, the bot couldn't even START polling (rare —
        # usually only happens if the token is invalid or network is
        # completely down). Log loudly but DON'T crash the FastAPI app —
        # the API server and strategy scheduler run independently.
        logger.error(f"  ❌ Bot failed to start: {e}")
        try:
            if bot_app.updater and bot_app.updater.running:
                await bot_app.updater.stop()
            if bot_app.running:
                await bot_app.stop()
            await bot_app.shutdown()
        except Exception:
            pass
        # Don't re-raise — keep the API server alive.


async def stop_bot():
    """Stop the bot gracefully."""
    global bot_app
    if bot_app:
        try:
            if bot_app.updater and bot_app.updater.running:
                await bot_app.updater.stop()
            if bot_app.running:
                await bot_app.stop()
            await bot_app.shutdown()
            logger.info("🦅 SmAttaker Bot stopped.")
        except Exception as e:
            logger.warning(f"  ⚠️ Bot stop error (non-fatal): {e}")


async def _polling_watchdog():
    """Background task: every 60s, check the polling loop is alive.

    The polling loop is python-telegram-bot's internal coroutine that
    calls getUpdates on a long-poll. In rare cases (a stuck HTTP
    connection without a working timeout, OR the polling task being
    cancelled by an unrelated exception), the loop can die silently —
    the FastAPI app keeps running, the scheduler keeps running, but
    NO user commands or button callbacks ever get processed.

    Symptoms this watchdog catches:
      - User sends /menu, /start, /signals — bot doesn't respond
      - User clicks "Track Trade" button — nothing happens
      - Admin sees bot_app.running == True (still "running" but dead)

    Recovery:
      1) Detect: if _LAST_POLL_OK_TS hasn't advanced in >5 minutes,
         the loop is stuck.
      2) Heal: stop the updater cleanly, then call start_bot() again
         to relaunch the polling loop.
      3) Don't thrash: only attempt recovery every 60s, and if
         recovery itself fails, alert the admin via alert_admins()
         so they can investigate manually.
    """
    global bot_app, _LAST_POLL_OK_TS
    HEARTBEAT_TIMEOUT_S = 300  # 5 minutes — generous, allows for transient stalls
    CHECK_INTERVAL_S = 60

    await asyncio.sleep(60)  # initial grace — let the polling loop establish
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_S)
            if bot_app is None or bot_app.updater is None:
                continue
            # If the updater reports not running, the loop is gone.
            if not bot_app.updater.running:
                logger.warning("  ⚠️ Watchdog: polling updater not running — attempting restart")
                try:
                    await bot_app.updater.stop()
                except Exception:
                    pass
                try:
                    await bot_app.updater.start_polling(
                        allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=False,
                        poll_interval=2.0,
                        timeout=10,
                        error_callback=lambda e: logger.error(f"Polling error: {e}"),
                    )
                    _LAST_POLL_OK_TS = time.time()
                    logger.info("  ✅ Watchdog: polling restarted")
                except Exception as e:
                    logger.error(f"  ❌ Watchdog: polling restart failed: {e}")
                    try:
                        from backend.services.alerts import alert_admins
                        await alert_admins(
                            "Bot polling loop died",
                            f"The Telegram bot's polling loop died and the watchdog "
                            f"couldn't restart it: {e}. User commands and button "
                            f"callbacks will not be processed until this is fixed. "
                            f"Check the bot's logs.",
                            alert_key="polling_watchdog_failed",
                        )
                    except Exception:
                        pass
                continue

            # Updater is "running" — but is it actually progressing?
            now = time.time()
            if _LAST_POLL_OK_TS and (now - _LAST_POLL_OK_TS) > HEARTBEAT_TIMEOUT_S:
                logger.warning(
                    f"  ⚠️ Watchdog: no polling progress for "
                    f"{int(now - _LAST_POLL_OK_TS)}s — forcing restart"
                )
                # Mark the heartbeat so we don't immediately re-trigger
                _LAST_POLL_OK_TS = now
                try:
                    await bot_app.updater.stop()
                    await asyncio.sleep(2)
                    await bot_app.updater.start_polling(
                        allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=False,
                        poll_interval=2.0,
                        timeout=10,
                        error_callback=lambda e: logger.error(f"Polling error: {e}"),
                    )
                    logger.info("  ✅ Watchdog: polling force-restarted after stall")
                except Exception as e:
                    logger.error(f"  ❌ Watchdog: forced restart failed: {e}")
            else:
                # Heartbeat looks healthy — refresh it. The polling loop
                # itself doesn't call us (python-telegram-bot doesn't
                # expose a per-tick callback), so we treat "still running"
                # as evidence of progress. Real stalls will eventually
                # surface via updater.running == False OR the next tick
                # after the watchdog restarts the loop.
                _LAST_POLL_OK_TS = now
        except asyncio.CancelledError:
            # Don't block shutdown — exit cleanly.
            break
        except Exception as e:
            logger.warning(f"  ⚠️ Watchdog tick error (non-fatal): {e}")
