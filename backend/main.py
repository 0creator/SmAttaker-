"""
SmAttaker — Main FastAPI Application
Entry point for the entire backend.
"""
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from backend.config import settings
from backend.database import init_db
from backend.redis_client import init_redis, close_redis
# v45: imported at top so the admin-stats endpoint (defined below) can
# use Depends(require_admin) without a NameError. Previously this was
# only imported near the bottom of the file, which forced late-binding
# patterns in any endpoint above that line.
from backend.api.auth import require_admin
from backend.models.user import User

# ⚠️ FIX: logging was never configured in this module. bot.py calls
# logging.basicConfig(), but it's only imported *inside* lifespan()
# (via `from backend.bot.bot import start_bot`), which runs AFTER the
# scheduler is set up. Worse, Python's logging.basicConfig is a no-op
# if the root logger already has handlers — and uvicorn installs its
# own handlers at startup. The net effect: every `logger.info(...)` in
# main.py and runner.py (strategy scheduler, "Running strategy
# engines...", signal counts) was silently discarded because the
# "smattaker.main" / "smattaker.runner" loggers inherited the root
# level (WARNING by default) with no handler formatting our messages.
# Configuring it explicitly HERE, before anything else runs, makes the
# scheduler and strategy logs actually visible in Render's log viewer —
# which is the only way to confirm the scheduler is alive without
# hitting the /api/system/scheduler-status diagnostic endpoint.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Make sure our own loggers propagate to the now-configured root.
for _name in ("smattaker", "smattaker.main", "smattaker.runner",
              "smattaker.signals", "apscheduler"):
    logging.getLogger(_name).setLevel(logging.INFO)

# ⚠️ SECURITY + NOISE FIX: python-telegram-bot uses httpx under the hood,
# and httpx logs EVERY request URL at INFO level — including the full
# Telegram bot token embedded in the URL path
# (https://api.telegram.org/bot<NUM>:<TOKEN>/getUpdates). This leaked
# the bot token to Render's log viewer, where anyone with log access
# could read it and take over the bot. httpx is also extremely chatty
# (one INFO line per API call), which buries our own logs.
#   - httpx       → WARNING: hides the token-leaking URLs, still shows real errors
#   - telegram    → WARNING: hides the per-request noise, keeps Conflict/network errors
#   - apscheduler.executors → INFO: keeps "Running job X" visible (already set above)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# Feed every ERROR+ log record (from anywhere in the app — bot,
# strategy runner, exchange connectors) into the in-memory recent-
# errors buffer so the admin panel can show "what broke recently"
# without anyone needing to go read Render logs. See error_tracker.py.
from backend.services.error_tracker import install_error_tracker
install_error_tracker()

logger = logging.getLogger("smattaker.main")
scheduler: AsyncIOScheduler | None = None

# ⚠️ Diagnostic state — lets us answer "did the scheduler actually run
# recently, and what happened?" via an HTTP call instead of guessing
# from whatever slice of logs happened to be captured. Render's log
# viewer only shows a moving window, so a quiet 10-15 minute log excerpt
# is NOT proof the scheduler stopped — this endpoint gives a definitive
# answer instead of everyone (including me) guessing from log snippets.
_scheduler_diagnostics = {
    "last_run_started_at": None,
    "last_run_finished_at": None,
    "last_run_error": None,
    "last_run_signal_counts": None,
    "total_runs": 0,
    # ── Black Swan (strategy #2) — same diagnostic contract, bs_ prefix ──
    "bs_last_run_started_at": None,
    "bs_last_run_finished_at": None,
    "bs_last_run_error": None,
    "bs_last_run_signal_counts": None,
    "bs_total_runs": 0,
}


async def _scheduled_black_swan_run():
    """Wrapper so the Black Swan APScheduler job never crashes silently and
    never overlaps. Isolated from _scheduled_strategy_run (V45) by design:
    own diagnostics, own timeout, own runner — zero shared mutable state.

    Cadence: every 30 minutes at :03/:33 UTC — 3 minutes after each :00/:30
    execution-bar open (the engine's 30m grid), giving data providers time
    to finalize the bar and keeping detection lag small and CONSISTENT.
    """
    from datetime import datetime, timezone
    from backend.strategies.black_swan_runner import run_black_swan
    _scheduler_diagnostics["bs_last_run_started_at"] = datetime.now(timezone.utc).isoformat()
    _scheduler_diagnostics["bs_total_runs"] += 1
    try:
        logger.info("⏱️  Scheduled Black Swan run starting...")
        # Same hard-wall-clock-timeout philosophy as the V45 job: a hung
        # network call must cancel, alert, and let the next tick proceed.
        # Black Swan analyzes only 2 assets (BTC+SOL, ~6000 30m bars each)
        # — a 600s cap gives >10x headroom over the typical ~10-40s run.
        result = await asyncio.wait_for(run_black_swan(), timeout=600)
        _scheduler_diagnostics["bs_last_run_error"] = None
        _scheduler_diagnostics["bs_last_run_signal_counts"] = result if isinstance(result, dict) else None
    except asyncio.TimeoutError:
        logger.error(
            "Scheduled Black Swan run TIMED OUT after 10 minutes — cancelled "
            "so the next 30-minute tick can still happen."
        )
        _scheduler_diagnostics["bs_last_run_error"] = "Timed out after 600s"
        from backend.services.alerts import alert_admins
        await alert_admins(
            "Black Swan run timed out",
            "The scheduled Black Swan run hit the 10-minute hard timeout and "
            "was cancelled. Check /api/system/scheduler-status and the logs.",
            alert_key="black_swan_run_timeout",
        )
    except Exception as e:
        logger.error(f"Scheduled Black Swan run failed: {e}", exc_info=True)
        _scheduler_diagnostics["bs_last_run_error"] = str(e)
        from backend.services.alerts import alert_admins
        await alert_admins(
            "Black Swan run failed",
            f"The scheduled Black Swan run raised an unhandled exception:\n`{str(e)[:500]}`",
            alert_key="black_swan_run_exception",
        )
    finally:
        _scheduler_diagnostics["bs_last_run_finished_at"] = datetime.now(timezone.utc).isoformat()


async def _scheduled_strategy_run():
    """Wrapper so APScheduler jobs never crash silently and never overlap."""
    from datetime import datetime, timezone
    from backend.strategies.runner import run_all_strategies
    _scheduler_diagnostics["last_run_started_at"] = datetime.now(timezone.utc).isoformat()
    _scheduler_diagnostics["total_runs"] += 1
    try:
        logger.info("⏱️  Scheduled strategy run starting...")
        # ⚠️ CRITICAL FIX: the scheduler is configured with max_instances=1
        # (see lifespan() below) — meaning if a single run ever hangs
        # (network call with no timeout, an await that never resolves,
        # etc.), NO future run can ever start again, forever, with
        # nothing in the logs to explain why ("silence" is exactly what
        # a hang looks like from the outside). A hard wall-clock timeout
        # here guarantees the job always finishes one way or another —
        # either with real results or a logged, actionable timeout error
        # — so the scheduler can never get permanently stuck again.
        #
        # ⚠️ V51 RESTORE: V45 lowered this from 600s → 240s (4 min).
        # That was wrong. The strategy runs every 15 minutes; 240s is
        # NOT enough headroom for 64 sequential asset fetches when
        # Yahoo/MEXC/Twelve Data are slow or rate-limiting (typical
        # worst case: 64 × 10s = 640s). V45's lowered timeout is the
        # direct cause of the "Strategy run timed out" alerts the user
        # has been receiving every ~45 minutes. Restoring the original
        # 600s (10 min) gives ample headroom while still leaving 5 min
        # of slack before the next scheduled run.
        #
        # ⚠️ v45.4.3 FIX: bumped from 600s → 900s (15 min). The 600s cap
        # was still being blown in production by cycles where: (a) 28+
        # stocks sequentially serialize through Twelve Data's 7/min rate
        # limit (each rate-limit wait can be up to ~13s, summing to
        # ~90s+ of pure sleeping per cycle), (b) US500/NAS100/US30/USOIL
        # each burned a rate-limit slot just to get back "needs Pro plan"
        # from Twelve Data (now fixed in data_fetcher.py via td_symbol
        # plumbing), (c) any Twelve-Data-timeout (e.g. QQQ in the field
        # logs) costs the full 15s timeout THEN a Yahoo fallback fetch
        # of 11k bars (now truncated to 1000 via the same fix). The
        # data_fetcher fixes should bring typical cycles back under 600s,
        # but raising the cap to 900s gives a 50% safety margin for the
        # worst-case combination of slow MEXC, exhausted Twelve Data
        # credits (full Yahoo fallback for every non-crypto symbol), and
        # Yahoo rate-limiting — without ever getting close to the next
        # hour's scheduled run.
        result = await asyncio.wait_for(run_all_strategies(), timeout=900)  # 15 min hard cap (v45.4.3)
        _scheduler_diagnostics["last_run_error"] = None
        _scheduler_diagnostics["last_run_signal_counts"] = result if isinstance(result, dict) else None
    except asyncio.TimeoutError:
        logger.error(
            "Scheduled strategy run TIMED OUT after 15 minutes — something is "
            "hanging (likely a network call with no/ineffective timeout). "
            "Cancelled so the next scheduled run can still happen."
        )
        _scheduler_diagnostics["last_run_error"] = "Timed out after 900s"
        from backend.services.alerts import alert_admins
        await alert_admins(
            "Strategy run timed out",
            "The scheduled strategy run hit the 15-minute hard timeout and was "
            "cancelled. Check /api/system/scheduler-status and the logs — this "
            "usually means a network call to an exchange/data provider is "
            "hanging without a working timeout.",
            alert_key="strategy_run_timeout",
        )
    except Exception as e:
        logger.error(f"Scheduled strategy run failed: {e}", exc_info=True)
        _scheduler_diagnostics["last_run_error"] = str(e)
        from backend.services.alerts import alert_admins
        await alert_admins(
            "Strategy run failed",
            f"The scheduled strategy run raised an unhandled exception:\n`{str(e)[:500]}`",
            alert_key="strategy_run_exception",
        )
    finally:
        _scheduler_diagnostics["last_run_finished_at"] = datetime.now(timezone.utc).isoformat()


async def _scheduled_discipline_evaluation():
    """Wrapper for the daily risk-discipline streak evaluation job.
    See backend/services/engagement.py for what this actually does and
    why it never touches P&L or trade volume."""
    from backend.database import async_session_factory
    from backend.services.engagement import evaluate_all_users_discipline
    try:
        async with async_session_factory() as db:
            result = await evaluate_all_users_discipline(db)
        logger.info(f"📊 Discipline evaluation: {result}")
    except Exception as e:
        logger.error(f"Discipline evaluation job failed: {e}", exc_info=True)


async def _scheduled_digest_dispatch():
    """Wrapper for the daily digest-dispatch job (sends weekly/monthly
    digests to whichever users are due, per their own cadence)."""
    from backend.database import async_session_factory
    from backend.services.engagement import send_due_digests
    try:
        async with async_session_factory() as db:
            result = await send_due_digests(db)
        logger.info(f"📬 Digest dispatch: {result}")
    except Exception as e:
        logger.error(f"Digest dispatch job failed: {e}", exc_info=True)


async def _scheduled_daily_report():
    """Wrapper for the once-daily platform performance report sent to
    every admin. See backend/services/daily_report.py for the rating
    methodology."""
    from backend.database import async_session_factory
    from backend.services.daily_report import send_daily_report
    try:
        async with async_session_factory() as db:
            result = await send_daily_report(db)
        logger.info(f"📊 Daily report: {result}")
    except Exception as e:
        logger.error(f"Daily report job failed: {e}", exc_info=True)


# ── Lifespan ────────────────────────────────────────────
def _startup_config_warnings():
    """Log loud, explicit warnings for any insecure default that's still
    in place at boot. None of these block startup (so a misconfigured
    deploy still serves traffic) but each one represents a real security
    hole if left unfixed in production.
    """
    import os
    warnings_emitted = 0

    # 1) SECRET_KEY still at the placeholder default — JWTs are signed
    #    with this, so the default means anyone who reads the source
    #    can forge admin tokens. Render auto-injects a random SECRET_KEY
    #    only if you've configured it as a env var; otherwise we're
    #    running with the placeholder.
    if not settings.SECRET_KEY or settings.SECRET_KEY == "change-me-in-production":
        logger.error(
            "🔒 SECURITY: SECRET_KEY is the placeholder default. "
            "JWTs are forgeable by anyone who reads the source code. "
            "Set a strong random SECRET_KEY in Render env vars NOW."
        )
        warnings_emitted += 1

    # 2) ENCRYPTION_KEY empty — exchange API keys can't be encrypted, so
    #    the encrypt_api_key() path will raise on first use. Better to
    #    flag it now than discover it when a user tries to connect an
    #    exchange.
    if not settings.ENCRYPTION_KEY:
        logger.warning(
            "🔒 CONFIG: ENCRYPTION_KEY is not set. "
            "Saving exchange API keys will fail until a Fernet key is "
            "configured. Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
        warnings_emitted += 1

    # 3) TELEGRAM_BOT_TOKEN empty — bot is dead on arrival. The lifespan
    #    still runs (so the API works) but no signals get broadcast.
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning(
            "🔒 CONFIG: TELEGRAM_BOT_TOKEN is not set. "
            "Bot commands and signal broadcasts will be skipped."
        )
        warnings_emitted += 1

    # 4) DEBUG=True in production — docs/redoc exposed, auto-reload on
    #    (which doubles the worker count and can cause the Conflict
    #    error we're trying to avoid).
    if settings.DEBUG:
        logger.warning(
            "🔒 SECURITY: DEBUG=True is on. /api/docs and /api/redoc are "
            "publicly accessible and uvicorn may run with reload (spawning "
            "a second worker that conflicts with the bot polling loop). "
            "Set DEBUG=False in production."
        )
        warnings_emitted += 1

    # 5) CORS wide-open with credentials — browsers ignore it, but it
    #    signals the CORS config was never tightened for the real frontend.
    if not settings.CORS_ALLOWED_ORIGINS and settings.APP_ENV == "production":
        logger.warning(
            "🔒 SECURITY: CORS_ALLOWED_ORIGINS is empty (allow_origins=['*']). "
            "Any website can call /api/* from the browser. Set it to your "
            "frontend URL(s) in production."
        )
        warnings_emitted += 1

    # 6) INTERNAL_API_KEY empty — any internal-only endpoint that uses
    #    verify_internal_api_key() will reject all calls (fail-closed
    #    is correct), but it also means those endpoints can't be used
    #    by anything, which usually indicates the deploy is incomplete.
    if not settings.INTERNAL_API_KEY:
        logger.info(
            "ℹ️  CONFIG: INTERNAL_API_KEY is not set. Internal endpoints "
            "will reject all calls (fail-closed). Set it if you need them."
        )

    if warnings_emitted:
        logger.warning(
            f"⚠️  Startup checks: {warnings_emitted} warning(s) above need attention."
        )
    else:
        logger.info("  ✅ Startup config checks passed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / Shutdown events."""
    logger.info("🦅 SmAttaker starting up...")

    # ── Startup security / config sanity checks ─────────────
    # ⚠️ These don't crash the app (so a misconfigured deploy can still
    # serve traffic while the admin fixes it) but they log loud warnings
    # so the issue is impossible to miss in the Render log viewer.
    _startup_config_warnings()

    await init_db()
    logger.info("  ✅ Database tables ready")
    await init_redis()
    logger.info("  ✅ Redis connected")

    # Start the Telegram bot in the background
    try:
        from backend.bot.bot import start_bot
        await start_bot()
        logger.info("  ✅ Telegram Bot running")
    except Exception as e:
        logger.warning(f"  ⚠️ Bot startup skipped: {e}")

    # ── Strategy Scheduler ──────────────────────────────
    # ⚠️ FIX: signals were NEVER generated automatically before this.
    # runner.py's docstring claimed "called by Celery Beat", but no
    # Celery worker/beat process existed anywhere in this project (not
    # in requirements' usage, not in render.yaml, not in any entrypoint).
    # The ONLY way signals were ever created was a human manually clicking
    # "Trigger Strategies" in the admin panel. That's why the bot's
    # /signals command always showed "No Active Signals" — there was
    # nothing populating the table on its own.
    global scheduler
    try:
        from apscheduler.triggers.cron import CronTrigger

        scheduler = AsyncIOScheduler(timezone="UTC")
        # ⚠️ CRITICAL FIX: the previous code used `next_run_time=None` which
        # in APScheduler 3.x means "never run this job". The interval job
        # would fire exactly ZERO times — the only run that ever happened
        # was the separate "date" one-shot job below, which is why users saw
        # signals appear ONCE at startup and then NOTHING for hours.
        #
        # ⚠️ TIMING FIX: this used to be a flat 15-minute INTERVAL, phase-
        # anchored to whenever the server happened to start/restart —
        # completely unaligned with the 1h candle boundaries the strategy
        # actually trades on (STRATEGY_TIMEFRAME = "1h"). Depending on
        # restart timing, a freshly-closed candle could sit unexamined for
        # up to ~15 minutes before the next poll even looked at it, on top
        # of the run's own ~5-6 minute processing time — up to ~20 minutes
        # of avoidable staleness between candle close and signal detection,
        # and it was inconsistent run-to-run since the phase depended on
        # server uptime. It also meant 3 out of every 4 runs re-analyzed
        # the SAME already-seen candle for zero benefit, burning data-
        # provider rate-limit budget (Twelve Data) for nothing.
        # Now anchored to the candle boundary itself: run once per hour,
        # 2 minutes past the hour — enough buffer for every data provider
        # to have finalized that hour's closing candle — so detection lag
        # is a small, CONSISTENT ~2 minutes instead of an arbitrary
        # up-to-15-minute drift, and provider calls drop by ~75%.
        scheduler.add_job(
            _scheduled_strategy_run,
            CronTrigger(minute=2, timezone="UTC"),
            id="strategy_run",
            max_instances=1,       # never run two cycles concurrently
            coalesce=True,         # if we fell behind, run once, not N times
        )
        # Fire one immediate run at startup too, so a fresh deploy/restart
        # doesn't sit idle for up to an hour waiting for the next :02 mark.
        scheduler.add_job(
            _scheduled_strategy_run,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=5),
            id="strategy_run_startup",
        )

        # ── Black Swan job (strategy #2) ─────────────────────────────────
        # Every 30 minutes at :03/:33 UTC, aligned to the engine's 30m
        # execution grid (XX:00/XX:30 exec-bar opens) with a 3-minute
        # finalization buffer. Isolated from the V45 job (own runner, own
        # timeout, own diagnostics). One startup one-shot, same pattern as
        # the V45 job above, so a fresh deploy doesn't wait for the next
        # :03/:33 mark. Respect BLACK_SWAN_ENABLED (checked inside the run
        # too — this only avoids registering a dead job).
        if getattr(settings, "BLACK_SWAN_ENABLED", True):
            scheduler.add_job(
                _scheduled_black_swan_run,
                CronTrigger(minute="3,33", timezone="UTC"),
                id="black_swan_run",
                max_instances=1,
                coalesce=True,
            )
            scheduler.add_job(
                _scheduled_black_swan_run,
                "date",
                run_date=datetime.now(timezone.utc) + timedelta(seconds=20),
                id="black_swan_run_startup",
            )

        # ── Signal Monitor Job ─────────────────────────────────
        # Watches every ACTIVE signal for SL/TP hits and 8-hour timeout.
        # Closes linked trades and notifies users with profit/loss.
        # This is the missing piece that made the system feel like an
        # "empty coffin" — without it, trades were never completed,
        # analytics stayed empty, and users never knew signal outcomes.
        from backend.services.signal_monitor import _scheduled_monitor_run, MONITOR_INTERVAL_SECONDS
        scheduler.add_job(
            _scheduled_monitor_run,
            "interval",
            seconds=MONITOR_INTERVAL_SECONDS,
            id="signal_monitor",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),  # 30s after startup
        )

        # ── Engagement Jobs ─────────────────────────────────
        # Discipline streak evaluation (yesterday's risk adherence) and
        # digest dispatch both run once daily. Neither needs to be more
        # frequent — "discipline" is inherently a daily-granularity
        # concept, and re-running mid-day would just re-evaluate the
        # same already-complete yesterday for no benefit.
        scheduler.add_job(
            _scheduled_discipline_evaluation,
            CronTrigger(hour=0, minute=10, timezone="UTC"),
            id="discipline_evaluation",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            _scheduled_digest_dispatch,
            CronTrigger(hour=8, minute=0, timezone="UTC"),
            id="digest_dispatch",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            _scheduled_daily_report,
            CronTrigger(hour=23, minute=55, timezone="UTC"),
            id="daily_report",
            max_instances=1,
            coalesce=True,
        )

        scheduler.start()
        logger.info(
            f"  ✅ Strategy scheduler running "
            f"(signals hourly at :02, monitor every {MONITOR_INTERVAL_SECONDS}s, "
            f"black swan every 30min at :03/:33)"
        )
    except Exception as e:
        logger.error(f"  ⚠️ Strategy scheduler failed to start: {e}", exc_info=True)

    logger.info(f"🦅 SmAttaker is LIVE on port {settings.PORT}")
    yield
    logger.info("🦅 SmAttaker shutting down...")
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    try:
        from backend.bot.bot import stop_bot
        await stop_bot()
    except Exception:
        pass
    await close_redis()
    logger.info("  ✅ Cleanup complete")


# ── App ─────────────────────────────────────────────────
app = FastAPI(
    title="SmAttaker Trading System",
    description="Elite trading signal & portfolio management system",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url="/api/redoc" if settings.DEBUG else None,
)

# ── Web templates (login / dashboard / admin) ────────────
# v52 REFACTOR: these three pages used to be ~3,550 lines of raw HTML
# embedded as Python triple-quoted strings directly in this file (the
# single biggest reason main.py had grown past 4,400 lines). They are
# now plain .html files under backend/templates/, rendered through
# Jinja2 — same output, but each page is independently readable,
# diffable, and editable without scrolling through unrelated backend
# logic. Nothing about the rendered HTML changed in this refactor.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ── CORS ────────────────────────────────────────────────
# ⚠️ FIX: `allow_origins=["*"]` combined with `allow_credentials=True` is
# invalid per the CORS spec (browsers reject/ignore the credentialed
# response in that combination) and is a sign this was never configured
# for a real deployment. If you have a specific frontend domain, put it
# in CORS_ALLOWED_ORIGINS; otherwise we keep credentials off with a
# wildcard so at least the config is internally consistent.
_cors_origins = [o.strip() for o in getattr(settings, "CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins if _cors_origins else ["*"],
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Health Check ────────────────────────────────────────
@app.api_route("/health", methods=["GET", "HEAD"], tags=["system"])
async def health_check():
    return {"status": "ok", "app": settings.APP_NAME, "version": "1.0.0"}


@app.get("/api/system/scheduler-status", tags=["system"])
async def scheduler_status():
    """
    Direct answer to "is the scheduler actually running, and what
    happened last time?" — no need to catch it in a live log window,
    which only ever shows whatever slice Render happens to be
    displaying and is easy to misread as "nothing is happening".

    Not admin-gated: contains no sensitive data (just timestamps and
    signal counts), and being freely checkable is the whole point of
    a diagnostic endpoint.
    """
    next_run = None
    monitor_next_run = None
    black_swan_next_run = None
    if scheduler is not None:
        job = scheduler.get_job("strategy_run")
        if job is not None and job.next_run_time is not None:
            next_run = job.next_run_time.isoformat()
        monitor_job = scheduler.get_job("signal_monitor")
        if monitor_job is not None and monitor_job.next_run_time is not None:
            monitor_next_run = monitor_job.next_run_time.isoformat()
        bs_job = scheduler.get_job("black_swan_run")
        if bs_job is not None and bs_job.next_run_time is not None:
            black_swan_next_run = bs_job.next_run_time.isoformat()

    # Signal monitor diagnostics
    try:
        from backend.services.signal_monitor import get_monitor_diagnostics
        monitor_diag = get_monitor_diagnostics()
    except Exception:
        monitor_diag = {}

    return {
        "scheduler_running": scheduler is not None and scheduler.running,
        # ⚠️ FIX: this used to report settings.STRATEGY_RUN_INTERVAL_MINUTES
        # (15), which is now stale — the strategy runs on an hourly cron
        # aligned to the candle boundary, not a flat interval. Report the
        # real schedule so this endpoint doesn't lie to whoever's reading it.
        "schedule": "hourly at :02 UTC (aligned to 1h candle close)",
        "next_scheduled_run": next_run,
        "monitor_next_run": monitor_next_run,
        "black_swan": {
            "enabled": bool(getattr(settings, "BLACK_SWAN_ENABLED", True)),
            "schedule": "every 30 min at :03/:33 UTC (aligned to 30m exec grid)",
            "next_scheduled_run": black_swan_next_run,
        },
        **_scheduler_diagnostics,
        "monitor": monitor_diag,
    }


# ── v45 Market Status endpoint ─────────────────────────
@app.get("/api/system/market-status", tags=["system"])
async def market_status():
    """Live market open/closed status for every supported asset class.

    Powers the admin dashboard's Market Status widget so the admin can
    see at a glance whether the strategy SHOULD be generating signals
    for each asset class right now. This is the same check the strategy
    runner uses to gate signal emission.

    Not admin-gated: it's just public market-hours info.
    """
    from backend.utils.market_hours import all_market_statuses
    return all_market_statuses()


# ── v52 Component Health endpoint ──────────────────────
@app.get("/api/system/health", tags=["system"])
async def system_health():
    """
    Single-call answer to "is the platform actually healthy right now?"
    — database + Redis reachability with real latency numbers, plus the
    scheduler status this endpoint already knew from scheduler-status.

    Same non-admin-gated philosophy as scheduler-status above: nothing
    here is sensitive (no query text, no stack traces — just up/down
    and latency), and a health check that requires a login is a health
    check most uptime monitors can't use.
    """
    from backend.services.health import check_database, check_redis, overall_status
    from backend.services.error_tracker import error_rate_last_hour

    db_health, redis_health = await check_database(), await check_redis()
    components = {
        "database": db_health,
        "redis": redis_health,
        "scheduler": {
            "status": "ok" if (scheduler is not None and scheduler.running) else "down",
        },
    }
    return {
        "status": overall_status(components),
        "components": components,
        "errors_last_hour": error_rate_last_hour(),
    }


# ── v52 Recent Errors endpoint (admin-only) ────────────
@app.get("/api/system/recent-errors", tags=["system"])
async def recent_errors(
    limit: int = 50,
    _admin: User = Depends(require_admin),
):
    """Last N ERROR+ log lines captured app-wide since the process
    started (in-memory only — resets on deploy/restart). Admin-gated
    because log lines can contain internal detail (file paths, partial
    stack context) that isn't meant for a public endpoint, unlike the
    up/down summary in /api/system/health above."""
    from backend.services.error_tracker import get_recent_errors
    return {"items": get_recent_errors(limit=limit)}


# ── v55 Live Portfolio WebSocket ───────────────────────
@app.websocket("/ws/portfolio")
async def ws_portfolio(websocket: WebSocket, token: str = "", account_type: str = "paper"):
    """
    Live-streams the authenticated user's open positions with
    continuously-updated unrealized P&L, plus a running equity number —
    the "live portfolio + price feed" surface that used to not exist at
    all (the dashboard only ever showed a static snapshot from the last
    page load).

    Auth via `?token=<JWT>` query param — WebSocket handshakes can't
    carry a normal Authorization header from browser JS, so the token
    goes in the query string instead (same pattern the /dashboard page
    itself already uses for its initial JWT handoff — see main.py's
    dashboard_page route). Rejected (close code 4001) if missing/invalid.

    Update cadence: every 4 seconds. Prices come from the SAME shared
    fetch path signal_monitor.py uses (backend/services/price_feed.py)
    — the number a user watches tick here is the same number that will
    trigger their TP/SL, not a different feed that could disagree.
    """
    from backend.utils.security import decode_token
    from backend.database import async_session_factory
    from sqlalchemy import select
    from backend.models.trade import Trade, TradeStatus
    from backend.services.price_feed import fetch_live_price
    from backend.services.trade_outcomes import compute_trade_pnl

    payload = decode_token(token) if token else None
    if not payload or payload.get("type") != "access":
        await websocket.close(code=4001, reason="Invalid or missing token")
        return
    user_id = payload.get("sub")
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token payload")
        return

    await websocket.accept()
    try:
        while True:
            # ⚠️ v56 FIX: the DB session used to stay open (checked out
            # from the pool) for the ENTIRE loop body below, including
            # the price-fetch loop's network calls — meaning a slow
            # external API response held a pooled DB connection hostage
            # for its whole duration, every 4 seconds, per connected
            # user. Under load that risks starving every other part of
            # the app (including the strategy scheduler) of DB
            # connections. Fixed: fetch what's needed from the DB, close
            # the session, THEN do the (potentially slow) network calls
            # with no DB connection held at all.
            async with async_session_factory() as db:
                user_result = await db.execute(select(User).where(User.id == user_id))
                user = user_result.scalar_one_or_none()
                if not user:
                    await websocket.close(code=4004, reason="User not found")
                    return

                trades_result = await db.execute(
                    select(Trade).where(
                        Trade.user_id == user.id,
                        Trade.account_type == account_type,
                        Trade.status == TradeStatus.ACTIVE,
                    )
                )
                open_trades = trades_result.scalars().all()
                base_balance = float(user.paper_balance or 10000.0) if account_type == "paper" else None

            # ── DB session closed/released above — everything from here
            # down is pure network I/O + in-memory computation, no DB. ──
            positions = []
            total_unrealized_usd = 0.0
            for t in open_trades:
                live_price = await fetch_live_price(t.symbol, t.asset_class)
                if live_price is None:
                    positions.append({
                        "id": str(t.id), "symbol": t.symbol, "direction": t.direction,
                        "entry_price": t.entry_price, "current_price": None,
                        "unrealized_pct": None, "unrealized_usd": None,
                        "status": "price_unavailable",
                    })
                    continue
                pnl = compute_trade_pnl(t, live_price)
                total_unrealized_usd += pnl["pnl_usd"]
                positions.append({
                    "id": str(t.id), "symbol": t.symbol, "direction": t.direction,
                    "asset_class": t.asset_class, "entry_price": t.entry_price,
                    "current_price": live_price, "stop_loss": t.stop_loss,
                    "unrealized_pct": pnl["pnl_pct"], "unrealized_usd": pnl["pnl_usd"],
                    "r_multiple": pnl["r_multiple"], "status": "live",
                })

            equity = (base_balance + total_unrealized_usd) if base_balance is not None else None

            await websocket.send_json({
                "type": "portfolio_update",
                "account_type": account_type,
                "positions": positions,
                "open_count": len(open_trades),
                "total_unrealized_usd": round(total_unrealized_usd, 2),
                "balance": base_balance,
                "equity": round(equity, 2) if equity is not None else None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            await asyncio.sleep(4)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"/ws/portfolio error for user {user_id}: {e}")
        try:
            await websocket.close(code=1011, reason="Internal error")
        except Exception:
            pass


# ── v45 Extended Analytics endpoint ────────────────────
@app.get("/api/system/admin-stats", tags=["system"])
async def admin_stats(
    _admin: User = Depends(require_admin),
):
    """Platform-wide KPIs for the admin dashboard's overview tab.

    Aggregates the most important numbers an admin needs at a glance:
      • user counts by status (active/trial/pending/banned/inactive)
      • total signals generated, broken down by status (active/executed/expired)
      • total trades tracked, broken down by status (active/completed)
      • platform-wide P&L (sum of pnl_usd on completed trades)
      • platform-wide win rate
      • active subscription count + MRR estimate
      • last 24h: signals generated, trades closed, users joined

    This is the data that makes the admin dashboard feel "professional"
    instead of "simplistic" — one call returns everything the overview
    cards need.
    """
    from sqlalchemy import select, func, case
    from backend.models.signal import Signal, SignalStatus
    from backend.models.trade import Trade, TradeStatus
    from backend.models.subscription import Subscription
    from backend.database import async_session_factory
    from datetime import datetime, timezone, timedelta

    async with async_session_factory() as db:
        # User counts by status
        user_counts_raw = (await db.execute(
            select(User.status, func.count(User.id)).group_by(User.status)
        )).all()
        user_counts = {row[0]: row[1] for row in user_counts_raw}
        total_users = sum(user_counts.values())

        # Signal counts by status
        sig_counts_raw = (await db.execute(
            select(Signal.status, func.count(Signal.id)).group_by(Signal.status)
        )).all()
        sig_counts = {row[0]: row[1] for row in sig_counts_raw}
        total_signals = sum(sig_counts.values())

        # Trade counts + P&L
        trade_counts_raw = (await db.execute(
            select(Trade.status, func.count(Trade.id), func.coalesce(func.sum(Trade.pnl_usd), 0.0))
            .group_by(Trade.status)
        )).all()
        trade_stats = {}
        total_pnl = 0.0
        total_wins = 0
        total_completed = 0
        for row in trade_counts_raw:
            status, cnt, pnl_sum = row[0], row[1], float(row[2] or 0.0)
            trade_stats[status] = {"count": cnt, "pnl_usd": pnl_sum}
            if status == TradeStatus.COMPLETED:
                total_pnl += pnl_sum
                total_completed += cnt
        # Win count — separate query for accuracy
        win_count = (await db.execute(
            select(func.count(Trade.id)).where(
                Trade.status == TradeStatus.COMPLETED,
                Trade.is_winner.is_(True),
            )
        )).scalar() or 0

        # Active subscriptions
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        active_subs_count = (await db.execute(
            select(func.count(Subscription.id)).where(
                Subscription.payment_status == "paid",
                Subscription.end_date.is_(None) | (Subscription.end_date >= now_utc),
            )
        )).scalar() or 0
        # MRR estimate (monthly + lifetime counted as 12 months)
        from sqlalchemy import case, extract
        mrr_result = (await db.execute(
            select(
                func.coalesce(func.sum(
                    case(
                        (Subscription.plan_type == "monthly", Subscription.amount_usd),
                        (Subscription.plan_type == "lifetime", Subscription.amount_usd / 12.0),
                        else_=0.0,
                    )
                ), 0.0)
            ).where(
                Subscription.payment_status == "paid",
                Subscription.end_date.is_(None) | (Subscription.end_date >= now_utc),
            )
        )).scalar() or 0.0

        # 24h activity
        cutoff_24h = now_utc - timedelta(hours=24)
        signals_24h = (await db.execute(
            select(func.count(Signal.id)).where(Signal.created_at >= cutoff_24h)
        )).scalar() or 0
        trades_closed_24h = (await db.execute(
            select(func.count(Trade.id)).where(
                Trade.status == TradeStatus.COMPLETED,
                Trade.exit_time >= cutoff_24h,
            )
        )).scalar() or 0
        users_joined_24h = (await db.execute(
            select(func.count(User.id)).where(User.created_at >= cutoff_24h)
        )).scalar() or 0

        return {
            "users": {
                "total": total_users,
                "by_status": user_counts,
            },
            "signals": {
                "total": total_signals,
                "by_status": sig_counts,
                "generated_24h": signals_24h,
            },
            "trades": {
                "by_status": trade_stats,
                "closed_24h": trades_closed_24h,
                "platform_pnl_usd": round(total_pnl, 2),
                "platform_win_rate": round(win_count / total_completed * 100, 1) if total_completed else 0.0,
                "platform_completed_trades": total_completed,
                "platform_winning_trades": win_count,
            },
            "subscriptions": {
                "active_count": active_subs_count,
                "estimated_mrr_usd": round(float(mrr_result), 2),
            },
            "users_joined_24h": users_joined_24h,
        }


@app.get("/api/system/admin-connections", tags=["system"])
async def admin_connections(
    _admin: User = Depends(require_admin),
):
    """v45: Live status of every user's exchange/MT5 connection.

    Returns one row per ExchangeConnection with:
      - user display info (telegram_id, username)
      - exchange name + label + testnet flag
      - stored connection_status (ok/error/unknown)
      - whether MT5 is in live (MetaApi) mode or stub mode
      - last_checked_at timestamp

    This powers the admin "Connection Health" panel so the operator can
    see at a glance which users have live MT5 bridges configured, which
    are in stub mode, and which connections are erroring.
    """
    from sqlalchemy import select
    from backend.models.exchange_connection import ExchangeConnection
    from backend.database import async_session_factory
    import re as _re
    _uuid_re = _re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

    async with async_session_factory() as db:
        result = await db.execute(
            select(ExchangeConnection, User)
            .join(User, ExchangeConnection.user_id == User.id)
            .order_by(ExchangeConnection.created_at.desc())
        )
        rows = result.all()

        items = []
        for conn, user in rows:
            # Detect MetaApi live mode: stored api_key is a UUID
            try:
                from backend.utils.security import decrypt_api_key
                api_key_plain = decrypt_api_key(conn.api_key_encrypted) if conn.api_key_encrypted else ""
            except Exception:
                api_key_plain = ""
            is_metaapi_live = conn.exchange_name.lower() == "mt5" and bool(_uuid_re.match(api_key_plain or ""))

            items.append({
                "id": str(conn.id),
                "user_id": str(user.id),
                "user_telegram_id": user.telegram_id,
                "user_username": user.telegram_username,
                "user_full_name": user.full_name,
                "exchange_name": conn.exchange_name,
                "exchange_label": conn.exchange_label,
                "is_testnet": conn.is_testnet,
                "is_active": conn.is_active,
                "connection_status": conn.connection_status,
                "connection_error": conn.connection_error,
                "last_checked_at": conn.last_checked_at.isoformat() if conn.last_checked_at else None,
                "is_metaapi_live": is_metaapi_live,
                "mode": "live" if is_metaapi_live else ("stub" if conn.exchange_name.lower() == "mt5" else "exchange"),
            })
        return {"items": items, "total": len(items)}


@app.get("/api/system/admin-mt5-balance/{connection_id}", tags=["system"])
async def admin_mt5_balance(
    connection_id: str,
    _admin: User = Depends(require_admin),
):
    """v45: Live-fetch the MT5 account balance for a specific connection.

    Uses the MetaApi Cloud SDK to read real balance/equity/margin from
    the user's MT5 account. Only works for connections in 'live' mode
    (MetaApi credentials stored). For stub-mode connections, returns
    a clear 'bridge not configured' message.
    """
    from sqlalchemy import select
    from backend.models.exchange_connection import ExchangeConnection
    from backend.database import async_session_factory
    from backend.exchange.mt5_connector import MT5Connector
    from backend.utils.security import decrypt_api_key
    import re as _re
    _uuid_re = _re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

    async with async_session_factory() as db:
        result = await db.execute(
            select(ExchangeConnection).where(ExchangeConnection.id == connection_id)
        )
        conn = result.scalar_one_or_none()
        if not conn:
            raise HTTPException(status_code=404, detail="Connection not found.")
        if conn.exchange_name.lower() != "mt5":
            raise HTTPException(status_code=400, detail="Not an MT5 connection.")

        try:
            api_key_plain = decrypt_api_key(conn.api_key_encrypted)
            secret_plain = decrypt_api_key(conn.secret_key_encrypted)
            server_plain = decrypt_api_key(conn.passphrase_encrypted) if conn.passphrase_encrypted else ""
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Decryption failed: {e}")

        is_metaapi = bool(_uuid_re.match(api_key_plain or ""))
        mt5 = MT5Connector(
            login=api_key_plain,
            password=secret_plain,
            server=server_plain,
            is_demo=conn.is_testnet,
            metaapi_account_id=api_key_plain if is_metaapi else None,
            metaapi_api_token=secret_plain if is_metaapi else None,
        )
        result = await mt5.fetch_balance()
        return result


@app.api_route("/", methods=["GET", "HEAD"], tags=["system"])
async def root_info():
    """
    Public root — intentionally NOT the admin panel.
    ⚠️ FIX: this used to serve the full admin dashboard (user management,
    trade journal, payment approval) to ANY anonymous visitor. The panel
    now lives at /admin, and every API call it makes requires an admin
    JWT (see the auth prompt in that page). This route just confirms the
    service is up.

    ⚠️ HEAD support: Render (and most uptime monitors) send a HEAD
    request as a cheap liveness check. Without an explicit HEAD route,
    FastAPI returns 405 Method Not Allowed, which logs as a "failure"
    in the health-check dashboard even though the service is fine.
    Using api_route with both methods lets one handler serve both —
    FastAPI/Starlette automatically strips the body for HEAD responses.
    """
    return {"status": "ok", "app": settings.APP_NAME, "admin_panel": "/admin"}


@app.get("/login", response_class=HTMLResponse, tags=["system"])
async def login_page(request: Request, token: str = ""):
    """Public sign-in page — real Telegram Login Widget, verified server-side.

    If a ?token= query parameter is present (e.g. an old bookmarked
    /login?token=<JWT> link from a previous version of the bot's
    'Open Web Dashboard' button), redirect straight to the dashboard
    instead of forcing the user through the widget again.
    """
    if token:
        # Don't render the login page at all — go straight to the
        # dashboard, which will save the token and load the app.
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/dashboard?token={token}", status_code=302)
    return templates.TemplateResponse(
        "login.html", {"request": request, "bot_username": settings.TELEGRAM_BOT_USERNAME}
    )


@app.get("/dashboard", response_class=HTMLResponse, tags=["system"])
async def dashboard_page(request: Request, token: str = ""):
    """User-facing web dashboard: profile, subscription, signals, trades,
    exchange connections, and risk settings — all wired to real endpoints
    under /api/account, /api/signals, and /api/trades.

    Accepts an optional ?token= query parameter: when the Telegram bot's
    /login command mints a JWT directly (bypassing the Telegram Login
    Widget, which requires the domain to be registered with BotFather),
    it sends a link to this URL with the token as a query param. The
    page JS reads it, saves it to localStorage, and strips it from the
    URL bar so the token isn't visible (or re-shared) after first load.
    """
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse, tags=["system"])
async def admin_panel(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})


# ── v45.4.2: Telegram Mini App — Live Signal Details ────────────────
# Serves the HTML page that powers the Telegram Mini App. The page
# loads signal data from /api/signals/{id}, renders a TradingView chart
# with the CORRECT exchange prefix (SNAP → NYSE:SNAP, not NASDAQ:SNAP),
# shows AI gauges + trade setup, and lets the user track the trade.
@app.get("/miniapp", response_class=HTMLResponse, tags=["system"])
async def miniapp_page(request: Request):
    """Telegram Mini App — Live signal details with TradingView chart.

    The URL is opened by Telegram when the user clicks the "Live Mini App"
    button on a signal message. The signal_id and user_id are passed as
    query parameters; the page uses Telegram's WebApp SDK for user
    identity, then fetches the full signal data from /api/signals/{id}.

    NOTE: this is a public route (no JWT required) — the Mini App page
    itself doesn't contain sensitive data. The actual signal details are
    fetched via /api/signals/{id} which validates the Telegram initData
    header (sent by the page JS) to ensure only authorized users see
    entry/SL/TP values.
    """
    return templates.TemplateResponse("miniapp.html", {"request": request})


# ── v45.4.2: Bot diagnostics endpoint ───────────────────────────────
# Allows the admin to verify the bot's polling loop is alive — useful
# for diagnosing "all buttons don't work" reports without needing to
# read Render's logs. Returns: bot_app running state, last poll heartbeat,
# watchdog status.
@app.get("/api/system/bot-status", tags=["system"])
async def bot_status_endpoint(_admin: User = Depends(require_admin)):
    """Diagnostic: is the Telegram bot's polling loop alive?

    Returns:
      - bot_initialized: True if bot_app has been created
      - updater_running: True if the polling loop is alive
      - last_poll_heartbeat: timestamp of last successful poll tick
      - first_boot: True if this is the first boot of the container
      (determines whether drop_pending_updates was applied)
    """
    from backend.bot.bot import bot_app, _LAST_POLL_OK_TS, _SENTINEL_FILE
    return {
        "bot_initialized": bot_app is not None,
        "updater_running": (
            bot_app is not None
            and bot_app.updater is not None
            and bot_app.updater.running
        ),
        "last_poll_heartbeat": _LAST_POLL_OK_TS,
        "first_boot": not _SENTINEL_FILE.exists(),
        "handlers_registered": bot_app is not None and len(bot_app.handlers) > 0
            if bot_app else False,
    }


# ── Mount All API Routers ───────────────────────────────
from backend.api.auth import router as auth_router
from backend.api.users import router as users_router
from backend.api.signals import router as signals_router
from backend.api.trades import router as trades_router
from backend.api.analytics import router as analytics_router
from backend.api.payments import router as payments_router, webhook_router
from backend.api.account import router as account_router
from backend.api.admin import router as admin_router

app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(users_router, prefix="/api/users", tags=["Users"])
app.include_router(signals_router, prefix="/api/signals", tags=["Signals"])
app.include_router(trades_router, prefix="/api/trades", tags=["Trades"])
app.include_router(analytics_router, prefix="/api/analytics", tags=["Analytics"])
app.include_router(payments_router, prefix="/api/payments", tags=["Payments"])
app.include_router(account_router, prefix="/api/account", tags=["Account"])
app.include_router(admin_router, prefix="/api/admin", tags=["Admin"])
app.include_router(webhook_router, prefix="/api/webhooks", tags=["Webhooks"])


# ── Manual Strategy Trigger ─────────────────────────────
@app.post("/api/strategies/run", tags=["Strategies"])
async def trigger_strategies(_admin=Depends(require_admin)):
    """Manually trigger all strategy engines to run. Admin-only.

    Note: this is now a *supplement* to the automatic scheduler in
    `lifespan()`, which already runs this hourly at :02 UTC (aligned to
    the 1h candle close). Use this button for an on-demand run, not as
    the only way signals get generated.
    """
    from backend.strategies.runner import run_all_strategies
    import asyncio
    asyncio.create_task(run_all_strategies())
    return {"status": "started", "message": "Strategies are running in background."}


# ── Legacy URL redirects ─────────────────────────────────
# Old bookmarks / bot buttons may point to /dashboard/login — redirect
# them to the canonical /login route so users land on the real sign-in
# page instead of seeing a 404. Same for /admin/login.
from fastapi.responses import RedirectResponse

@app.get("/dashboard/login", include_in_schema=False)
async def redirect_dashboard_login(token: str = ""):
    """Legacy /dashboard/login → /login (with token passthrough)."""
    target = "/login"
    if token:
        target += f"?token={token}"
    return RedirectResponse(url=target, status_code=302)


@app.get("/admin/login", include_in_schema=False)
async def redirect_admin_login():
    """Legacy /admin/login → /admin (admin panel itself)."""
    return RedirectResponse(url="/admin", status_code=302)


@app.get("/dashboard/signup", include_in_schema=False)
async def redirect_dashboard_signup():
    """Legacy /dashboard/signup → /login (signup is via Telegram)."""
    return RedirectResponse(url="/login", status_code=302)


# ── Favicon (inline SVG data URL — no static files needed) ──
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Return a tiny SVG favicon as an ICO-replacement so browsers
    don't log a 404 on every page load. The SVG is rendered as a gold
    'S' on a dark background — matches the brand shown on /login."""
    from fastapi.responses import Response
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#05070D"/>'
        '<text x="16" y="22" font-family="Arial, sans-serif" font-size="20" '
        'font-weight="900" text-anchor="middle" fill="#D4AF37">S</text>'
        '</svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


# ── Catch-all 404 handler ───────────────────────────────
from fastapi import Request as _Request
from fastapi.responses import JSONResponse as _JSONResponse


@app.exception_handler(404)
async def not_found_handler(request: _Request, exc):
    """Return a JSON 404 for /api/* routes, but a friendly HTML page
    for unknown web routes — so users hitting a stale bookmark see a
    helpful message + link back to /login instead of a bare 404."""
    path = request.url.path
    if path.startswith("/api/"):
        return _JSONResponse(
            status_code=404,
            content={"detail": f"Not found: {path}", "code": "not_found"},
        )
    # HTML fallback for web routes
    from fastapi.responses import HTMLResponse as _HTMLResponse
    return _HTMLResponse(
        status_code=404,
        content="""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmAttaker — Page Not Found</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen flex items-center justify-center bg-slate-950 text-slate-200 px-4">
  <div class="text-center max-w-md">
    <div class="text-7xl font-black text-amber-500 mb-4">404</div>
    <h1 class="text-2xl font-bold text-white mb-2">Page not found</h1>
    <p class="text-slate-400 mb-6">The page you're looking for doesn't exist or has moved.</p>
    <a href="/login" class="inline-block bg-amber-500 hover:bg-amber-600 text-slate-900 font-bold px-6 py-3 rounded-lg transition">
      Go to Sign In
    </a>
  </div>
</body>
</html>
""",
    )


# ── Global exception handler ────────────────────────────
# ⚠️ CRITICAL FIX: there was no handler for unhandled exceptions
# anywhere in the app — only the 404 handler above. Starlette's default
# behavior for an unhandled exception (in production, debug=False) is
# to return a PLAIN TEXT "Internal Server Error" body, not JSON. Every
# admin-panel action does `const j = await res.json()` (sometimes
# guarded with .catch(() => ({})), sometimes not) expecting a JSON
# error body with a `detail` field. A plain-text 500 makes res.json()
# itself throw — which is exactly what surfaces to the user as
# "Network error" (caught by the outer try/catch) or "Update failed"
# (the .catch(() => ({})) swallows it, j.detail is undefined, falls
# back to that default string). This was happening for ANY unhandled
# exception in ANY endpoint — not a bug specific to one route. This
# handler guarantees every unhandled exception, everywhere in the app,
# still comes back as valid JSON with a real detail message, AND logs
# the full traceback server-side (previously lost entirely), AND
# alerts the admin so unknown-unknowns actually surface instead of
# just looking like "the button doesn't work" with zero diagnostics.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: _Request, exc: Exception):
    import logging as _lg
    import traceback as _tb

    path = request.url.path
    tb_str = _tb.format_exc()
    _lg.getLogger("smattaker.unhandled").error(
        f"Unhandled exception on {request.method} {path}: {exc}\n{tb_str}"
    )

    # Best-effort admin alert — never let alerting itself break the
    # error response.
    try:
        from backend.database import async_session_factory as _asf
        from backend.models.admin_notification import AdminNotification as _AN, NotificationType as _NT
        async with _asf() as _ndb:
            _ndb.add(_AN(
                notification_type=_NT.SYSTEM_ERROR,
                title=f"Unhandled exception: {request.method} {path}",
                message=f"{exc}\n\n{tb_str[-1500:]}",
                severity="critical",
            ))
            await _ndb.commit()
    except Exception:
        pass

    if path.startswith("/api/"):
        # Include a short exception summary in the response itself.
        # This is a private admin/internal API (not a consumer-facing
        # product), and we've now spent several rounds chasing "network
        # error" / "update failed" reports with zero diagnostic detail
        # reaching either the user or me. A one-line summary here means
        # the NEXT occurrence shows the actual error directly in the
        # toast — turning a guessing game into an instant diagnosis.
        return _JSONResponse(
            status_code=500,
            content={
                "detail": f"Unexpected error: {type(exc).__name__}: {str(exc)[:300]}",
                "code": "internal_error",
            },
        )
    from fastapi.responses import HTMLResponse as _HTMLResponse
    return _HTMLResponse(
        status_code=500,
        content="""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmAttaker — Something Went Wrong</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen flex items-center justify-center bg-slate-950 text-slate-200 px-4">
  <div class="text-center max-w-md">
    <div class="text-6xl mb-4">⚠️</div>
    <h1 class="text-2xl font-bold text-white mb-2">Something went wrong</h1>
    <p class="text-slate-400 mb-6">An unexpected error occurred. The team has been notified.</p>
    <a href="/login" class="inline-block bg-amber-500 hover:bg-amber-600 text-slate-900 font-bold px-6 py-3 rounded-lg transition">
      Go to Sign In
    </a>
  </div>
</body>
</html>
""",
    )


# ── Run server ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=settings.DEBUG,
    )
