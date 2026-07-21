"""
SmAttaker — Main FastAPI Application
Entry point for the entire backend.
"""
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from backend.config import settings
from backend.database import init_db
from backend.redis_client import init_redis, close_redis

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
}


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
        result = await asyncio.wait_for(run_all_strategies(), timeout=600)  # 10 min hard cap
        _scheduler_diagnostics["last_run_error"] = None
        _scheduler_diagnostics["last_run_signal_counts"] = result if isinstance(result, dict) else None
    except asyncio.TimeoutError:
        logger.error(
            "Scheduled strategy run TIMED OUT after 10 minutes — something is "
            "hanging (likely a network call with no/ineffective timeout). "
            "Cancelled so the next scheduled run can still happen."
        )
        _scheduler_diagnostics["last_run_error"] = "Timed out after 600s"
        from backend.services.alerts import alert_admins
        await alert_admins(
            "Strategy run timed out",
            "The scheduled strategy run hit the 10-minute hard timeout and was "
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


# ── Lifespan ────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / Shutdown events."""
    logger.info("🦅 SmAttaker starting up...")
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
        scheduler = AsyncIOScheduler(timezone="UTC")
        # ⚠️ CRITICAL FIX: the previous code used `next_run_time=None` which
        # in APScheduler 3.x means "never run this job". The interval job
        # would fire exactly ZERO times — the only run that ever happened
        # was the separate "date" one-shot job below, which is why users saw
        # signals appear ONCE at startup and then NOTHING for hours.
        # Setting next_run_time=now makes the first run fire immediately and
        # every subsequent run fire on the interval (15 min) forever.
        scheduler.add_job(
            _scheduled_strategy_run,
            "interval",
            minutes=settings.STRATEGY_RUN_INTERVAL_MINUTES,
            id="strategy_run",
            max_instances=1,       # never run two cycles concurrently
            coalesce=True,         # if we fell behind, run once, not N times
            next_run_time=datetime.now(timezone.utc),  # fire first run immediately, then every interval
        )
        scheduler.start()
        # NOTE: the old separate "date" one-shot job is no longer needed —
        # the interval job now fires its first run immediately (next_run_time
        # = now) and then repeats every STRATEGY_RUN_INTERVAL_MINUTES.
        logger.info(
            f"  ✅ Strategy scheduler running "
            f"(every {settings.STRATEGY_RUN_INTERVAL_MINUTES} min)"
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
@app.get("/health", tags=["system"])
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
    if scheduler is not None:
        job = scheduler.get_job("strategy_run")
        if job is not None and job.next_run_time is not None:
            next_run = job.next_run_time.isoformat()

    return {
        "scheduler_running": scheduler is not None and scheduler.running,
        "configured_interval_minutes": settings.STRATEGY_RUN_INTERVAL_MINUTES,
        "next_scheduled_run": next_run,
        **_scheduler_diagnostics,
    }


@app.get("/", tags=["system"])
async def root_info():
    """
    Public root — intentionally NOT the admin panel.
    ⚠️ FIX: this used to serve the full admin dashboard (user management,
    trade journal, payment approval) to ANY anonymous visitor. The panel
    now lives at /admin, and every API call it makes requires an admin
    JWT (see the auth prompt in that page). This route just confirms the
    service is up.
    """
    return {"status": "ok", "app": settings.APP_NAME, "admin_panel": "/admin"}


@app.get("/login", response_class=HTMLResponse, tags=["system"])
async def login_page(token: str = ""):
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
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SmAttaker — Sign In</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
    <script>
        tailwind.config = { theme: { extend: {
            fontFamily: { sans: ['Manrope','sans-serif'], display: ['Space Grotesk','sans-serif'] },
            colors: { void: '#05070D', card: '#10162A', line: '#1E2740', gold: { DEFAULT: '#D4AF37', light: '#F0D683' } }
        }}}
    </script>
    <style>
        body { background: radial-gradient(1000px 500px at 50% -10%, rgba(212,175,55,0.10), transparent), #05070D; }
        .gold-text { background: linear-gradient(135deg, #F0D683 0%, #D4AF37 45%, #9C7A1E 100%); -webkit-background-clip: text; background-clip: text; color: transparent; }
        .gold-btn { background: linear-gradient(135deg, #F0D683 0%, #D4AF37 60%, #C9A227 100%); }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center font-sans text-slate-200 px-4">

    <div class="w-full max-w-sm">
        <div class="flex flex-col items-center mb-8">
            <div class="w-16 h-16 rounded-2xl gold-btn flex items-center justify-center shadow-lg mb-4">
                <span class="text-void text-2xl font-black">S</span>
            </div>
            <h1 class="font-display font-bold text-2xl gold-text">SMATTAKER</h1>
            <p class="text-xs text-slate-500 mt-1 uppercase tracking-widest">Institutional Trading Access</p>
        </div>

        <div class="bg-card border border-line rounded-2xl p-8 shadow-2xl text-center">
            <h2 class="font-bold text-lg text-white mb-1">Sign in with Telegram</h2>
            <p class="text-sm text-slate-500 mb-6">
                Your account is verified through Telegram — the same identity your bot already knows.
                No separate password to create or lose.
            </p>

            <div id="telegramWidgetContainer" class="flex justify-center mb-4"></div>

            <div id="loadingState" class="hidden items-center justify-center gap-2 text-gold text-sm font-semibold py-3">
                <svg class="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>
                Verifying your Telegram identity...
            </div>

            <div id="errorState" class="hidden bg-red-500/10 border border-red-500/30 text-red-400 text-xs rounded-lg p-3 mb-2"></div>

            <label class="flex items-center gap-2 text-xs text-slate-400 mt-4 cursor-pointer select-none">
                <input type="checkbox" id="rememberMe" checked class="accent-amber-500">
                Keep me signed in on this device (7 days)
            </label>

            <p class="text-[11px] text-slate-600 mt-4 leading-relaxed">
                New here? Signing in for the first time automatically starts your registration —
                an admin will review and approve your account shortly after.
            </p>
        </div>

        <div class="bg-card border border-gold/20 rounded-xl p-4 mt-6 text-center">
            <p class="text-sm text-slate-300 font-semibold mb-2">🔗 Easiest way to log in</p>
            <p class="text-xs text-slate-400 leading-relaxed mb-3">
                If the button above doesn't appear or doesn't work, just
                send <span class="text-gold font-bold">/login</span> to
                <span class="text-gold">@__BOT_USERNAME__</span> on Telegram.
                You'll get a direct link to your dashboard — no password,
                no widget, one tap.
            </p>
        </div>
    </div>

    <script>
        // ── Session persistence helpers ──
        // The dashboard JWT is valid for 7 days (see JWT_ACCESS_TOKEN_EXPIRE_MINUTES).
        // We keep it in localStorage (survives new tabs + browser
        // restarts) with a recorded timestamp so we can expire it
        // locally even if the server-side expiry isn't checked yet.
        // 'Remember me' unchecked => fall back to sessionStorage for
        // users who explicitly don't want the token persisted to disk.
        const SESSION_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
        function _saveSession(token, refreshToken) {
            const remember = document.getElementById('rememberMe') && document.getElementById('rememberMe').checked;
            const store = remember ? localStorage : sessionStorage;
            // Mirror into both so a pre-existing sessionStorage copy
            // doesn't override the new localStorage one on next load.
            try { sessionStorage.removeItem('smattaker_user_token'); } catch (e) {}
            try { sessionStorage.removeItem('smattaker_refresh_token'); } catch (e) {}
            try { sessionStorage.removeItem('smattaker_session_ts'); } catch (e) {}
            store.setItem('smattaker_user_token', token);
            if (refreshToken) store.setItem('smattaker_refresh_token', refreshToken);
            store.setItem('smattaker_session_ts', Date.now().toString());
        }
        function _loadSession() {
            let token = localStorage.getItem('smattaker_user_token');
            let ts = localStorage.getItem('smattaker_session_ts');
            let store = localStorage;
            if (!token) {
                token = sessionStorage.getItem('smattaker_user_token');
                ts = sessionStorage.getItem('smattaker_session_ts');
                store = sessionStorage;
            }
            if (!token) return null;
            // Expire locally after SESSION_MAX_AGE_MS so we don't keep
            // a stale token around indefinitely.
            if (ts && (Date.now() - parseInt(ts, 10)) > SESSION_MAX_AGE_MS) {
                store.removeItem('smattaker_user_token');
                store.removeItem('smattaker_refresh_token');
                store.removeItem('smattaker_session_ts');
                return null;
            }
            return token;
        }
        function _clearSession() {
            [localStorage, sessionStorage].forEach(s => {
                try { s.removeItem('smattaker_user_token'); } catch (e) {}
                try { s.removeItem('smattaker_refresh_token'); } catch (e) {}
                try { s.removeItem('smattaker_session_ts'); } catch (e) {}
            });
        }

        // ── Render the OFFICIAL Telegram Login Widget ──────────
        // This is Telegram's own script; it's the only way a web page can
        // legitimately prove a visitor controls a given Telegram account.
        // The widget calls `onTelegramAuth` with a payload signed by
        // Telegram (hash + auth_date), which our backend verifies with
        // HMAC-SHA256 against the bot token (see utils/security.py).
        const script = document.createElement('script');
        script.src = 'https://telegram.org/js/telegram-widget.js?22';
        script.setAttribute('data-telegram-login', '__BOT_USERNAME__');
        script.setAttribute('data-size', 'large');
        script.setAttribute('data-radius', '10');
        script.setAttribute('data-onauth', 'onTelegramAuth(user)');
        script.setAttribute('data-request-access', 'write');
        document.getElementById('telegramWidgetContainer').appendChild(script);

        async function onTelegramAuth(user) {
            document.getElementById('telegramWidgetContainer').classList.add('hidden');
            document.getElementById('loadingState').classList.remove('hidden');
            document.getElementById('loadingState').classList.add('flex');
            document.getElementById('errorState').classList.add('hidden');

            try {
                const res = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(user),
                });
                const json = await res.json();

                if (!res.ok) {
                    throw new Error(json.detail || 'Login failed. Please try again.');
                }

                // ⚠️ Match the REAL response shape from api/auth.py exactly:
                // { data: { token, refresh_token, user: { status, ... }, is_new } }
                const token = json.data && json.data.token;
                const refreshToken = json.data && json.data.refresh_token;
                const userStatus = json.data && json.data.user && json.data.user.status;

                if (token && userStatus === 'pending_approval') {
                    // Store the token anyway (dashboard can still show
                    // "awaiting approval" state using it), but lead with
                    // the approval message so it's not mistaken for a bug.
                    _saveSession(token, refreshToken);
                    document.getElementById('loadingState').classList.add('hidden');
                    document.getElementById('errorState').classList.remove('hidden');
                    document.getElementById('errorState').className = 'bg-gold/10 border border-gold/30 text-gold text-xs rounded-lg p-3 mb-2';
                    document.getElementById('errorState').textContent =
                        '✅ Account created! An admin needs to approve it before signals/trading unlock — you\'ll get a Telegram message once approved. Redirecting to your dashboard...';
                    setTimeout(() => { window.location.href = '/dashboard'; }, 2500);
                } else if (token) {
                    _saveSession(token, refreshToken);
                    window.location.href = '/dashboard';
                } else {
                    throw new Error('Unexpected response from server.');
                }
            } catch (e) {
                document.getElementById('loadingState').classList.add('hidden');
                document.getElementById('errorState').classList.remove('hidden');
                document.getElementById('errorState').textContent = e.message;
                document.getElementById('telegramWidgetContainer').classList.remove('hidden');
            }
        }

        // If already logged in, skip straight to the dashboard.
        // If already logged in (localStorage survives across tabs /
        // restarts, sessionStorage does not), skip straight to the
        // dashboard. This is what stops the 'Open Web Dashboard'
        // button from forcing a re-login every single time.
        if (_loadSession()) {
            window.location.href = '/dashboard';
        }
    </script>
</body>
</html>

"""
    html_content = html_content.replace("__BOT_USERNAME__", settings.TELEGRAM_BOT_USERNAME)
    return HTMLResponse(content=html_content)


@app.get("/dashboard", response_class=HTMLResponse, tags=["system"])
async def dashboard_page(token: str = ""):
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
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SmAttaker — My Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
    <script>
        tailwind.config = { theme: { extend: {
            fontFamily: { sans: ['Manrope','sans-serif'], display: ['Space Grotesk','sans-serif'] },
            colors: { void: '#05070D', panel: '#0B0F1A', card: '#10162A', line: '#1E2740',
                gold: { DEFAULT: '#D4AF37', light: '#F0D683' }, win: '#22C55E', loss: '#EF4444', warn: '#F59E0B', muted: '#8B93A8' }
        }}}
    </script>
    <style>
        body { background: radial-gradient(1200px 600px at 15% -10%, rgba(212,175,55,0.08), transparent), #05070D; }
        .gold-text { background: linear-gradient(135deg, #F0D683 0%, #D4AF37 45%, #9C7A1E 100%); -webkit-background-clip: text; background-clip: text; color: transparent; }
        .gold-btn { background: linear-gradient(135deg, #F0D683 0%, #D4AF37 60%, #C9A227 100%); }
        .tab-panel { display: none; } .tab-panel.active { display: block; }
        .badge { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px; }
        input, select { background: #0B0F1A; }
    </style>
</head>
<body class="text-slate-200 font-sans min-h-screen">

<div id="approvalBanner" class="hidden bg-warn/10 border-b border-warn/30 text-warn text-sm text-center py-2 px-4"></div>

<header class="border-b border-line px-6 py-4 flex items-center justify-between sticky top-0 bg-void/90 backdrop-blur z-20">
    <div class="flex items-center gap-3">
        <div class="w-9 h-9 rounded-xl gold-btn flex items-center justify-center"><span class="text-void font-black">S</span></div>
        <span class="font-display font-bold gold-text">SMATTAKER</span>
    </div>
    <div class="flex items-center gap-4">
        <span id="userGreeting" class="text-sm text-slate-400 hidden sm:block"></span>
        <button onclick="logout()" class="text-xs text-muted hover:text-loss transition"><i class="fa-solid fa-right-from-bracket mr-1"></i>Sign out</button>
    </div>
</header>

<nav class="flex gap-1 px-6 pt-4 overflow-x-auto border-b border-line" id="navTabs">
    <button class="nav-tab px-4 py-2.5 rounded-t-lg text-sm font-semibold text-gold border-b-2 border-gold" data-tab="overview">Overview</button>
    <button class="nav-tab px-4 py-2.5 rounded-t-lg text-sm font-semibold text-slate-400 border-b-2 border-transparent" data-tab="signals">Signals</button>
    <button class="nav-tab px-4 py-2.5 rounded-t-lg text-sm font-semibold text-slate-400 border-b-2 border-transparent" data-tab="trades">My Trades</button>
    <button class="nav-tab px-4 py-2.5 rounded-t-lg text-sm font-semibold text-slate-400 border-b-2 border-transparent" data-tab="exchanges">Exchanges</button>
    <button class="nav-tab px-4 py-2.5 rounded-t-lg text-sm font-semibold text-slate-400 border-b-2 border-transparent" data-tab="risk">Risk Settings</button>
</nav>

<main class="p-6 max-w-5xl mx-auto space-y-6">

    <!-- OVERVIEW -->
    <section id="tab-overview" class="tab-panel active space-y-5">
        <div class="grid grid-cols-1 md:grid-cols-3 gap-5">
            <div class="bg-card border border-line rounded-2xl p-5 md:col-span-2">
                <h3 class="font-bold text-white mb-3">Subscription Status</h3>
                <div id="subscriptionBlock" class="text-sm text-muted">Loading...</div>
            </div>
            <div class="bg-card border border-line rounded-2xl p-5">
                <h3 class="font-bold text-white mb-3">Account</h3>
                <div id="accountBlock" class="text-sm space-y-2"></div>
            </div>
        </div>
        <div class="bg-card border border-line rounded-2xl p-5">
            <h3 class="font-bold text-white mb-3">Quick Links</h3>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                <button onclick="switchTab('signals')" class="bg-void/50 hover:bg-void border border-line rounded-xl p-4 text-left transition"><i class="fa-solid fa-bolt text-gold mb-2 block"></i>View Signals</button>
                <button onclick="switchTab('trades')" class="bg-void/50 hover:bg-void border border-line rounded-xl p-4 text-left transition"><i class="fa-solid fa-scale-balanced text-gold mb-2 block"></i>My Trades</button>
                <button onclick="switchTab('exchanges')" class="bg-void/50 hover:bg-void border border-line rounded-xl p-4 text-left transition"><i class="fa-solid fa-plug text-gold mb-2 block"></i>Connect Exchange</button>
                <button onclick="switchTab('risk')" class="bg-void/50 hover:bg-void border border-line rounded-xl p-4 text-left transition"><i class="fa-solid fa-shield-halved text-gold mb-2 block"></i>Risk Settings</button>
            </div>
        </div>
    </section>

    <!-- SIGNALS -->
    <section id="tab-signals" class="tab-panel space-y-4">
        <div class="flex items-center justify-between">
            <div class="text-xs text-muted">Auto-refreshing every 60s <span id="signalsUpdated" class="text-slate-500"></span></div>
        </div>
        <div id="signalsList" class="grid grid-cols-1 md:grid-cols-2 gap-4"></div>
    </section>

    <!-- TRADES -->
    <section id="tab-trades" class="tab-panel">
        <div class="bg-card border border-line rounded-2xl overflow-hidden">
            <table class="w-full text-sm">
                <thead class="bg-void/60 text-muted text-xs uppercase"><tr>
                    <th class="text-left px-4 py-3">Symbol</th><th class="text-left px-4 py-3">Direction</th>
                    <th class="text-right px-4 py-3">Entry</th><th class="text-right px-4 py-3">Exit</th>
                    <th class="text-right px-4 py-3">P&L%</th><th class="text-center px-4 py-3">Status</th>
                </tr></thead>
                <tbody id="myTradesBody"></tbody>
            </table>
        </div>
    </section>

    <!-- EXCHANGES -->
    <section id="tab-exchanges" class="tab-panel space-y-5">
        <div class="bg-card border border-line rounded-2xl p-5">
            <h3 class="font-bold text-white mb-4">Connect a New Exchange</h3>
            <form id="exchangeForm" class="grid grid-cols-1 md:grid-cols-2 gap-3">
                <select id="exExchange" required class="border border-line rounded-lg px-3 py-2.5 text-sm">
                    <option value="">Select exchange...</option>
                    <option value="mexc">MEXC (recommended)</option>
                    <option value="kucoin">KuCoin (recommended)</option>
                    <option value="bybit">Bybit</option>
                    <option value="okx">OKX</option>
                    <option value="binance">Binance</option>
                    <option value="kraken">Kraken</option>
                    <option value="bitget">Bitget</option>
                </select>
                <input id="exLabel" placeholder="Label (e.g. My Main Account)" class="border border-line rounded-lg px-3 py-2.5 text-sm">
                <input id="exApiKey" required placeholder="API Key" class="border border-line rounded-lg px-3 py-2.5 text-sm md:col-span-2">
                <input id="exSecretKey" required type="password" placeholder="Secret Key" class="border border-line rounded-lg px-3 py-2.5 text-sm md:col-span-2">
                <input id="exPassphrase" type="password" placeholder="Passphrase (OKX/Coinbase only)" class="border border-line rounded-lg px-3 py-2.5 text-sm md:col-span-2">
                <label class="flex items-center gap-2 text-sm text-muted md:col-span-2"><input type="checkbox" id="exTestnet"> Use testnet/sandbox</label>
                <button type="submit" class="gold-btn text-void font-bold text-sm px-4 py-2.5 rounded-lg md:col-span-2">Connect Exchange</button>
            </form>
            <p class="text-xs text-muted mt-3"><i class="fa-solid fa-lock mr-1"></i>Keys are encrypted at rest and never displayed again after saving.</p>
        </div>
        <div id="exchangesList" class="space-y-3"></div>
    </section>

    <!-- RISK SETTINGS -->
    <section id="tab-risk" class="tab-panel space-y-5">
        <div class="bg-card border border-line rounded-2xl p-5">
            <h3 class="font-bold text-white mb-4">Risk Configuration</h3>
            <form id="riskForm" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                    <label class="text-xs text-muted block mb-1">Account Type</label>
                    <select id="riskAccountType" class="w-full border border-line rounded-lg px-3 py-2.5 text-sm">
                        <option value="demo">Demo</option>
                        <option value="real">Real</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-muted block mb-1">Position Sizing Method</label>
                    <select id="riskSizingMethod" class="w-full border border-line rounded-lg px-3 py-2.5 text-sm">
                        <option value="risk_based">Risk-% Based (recommended)</option>
                        <option value="fixed">Fixed USD Amount</option>
                        <option value="fractional">Fractional</option>
                        <option value="kelly">Kelly Criterion</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-muted block mb-1">Max Risk per Trade (%)</label>
                    <input id="riskMaxPerTrade" type="number" step="0.1" min="0.1" max="10" class="w-full border border-line rounded-lg px-3 py-2.5 text-sm">
                </div>
                <div>
                    <label class="text-xs text-muted block mb-1">Fixed Position Size (USD)</label>
                    <input id="riskFixedSize" type="number" step="1" min="1" class="w-full border border-line rounded-lg px-3 py-2.5 text-sm">
                </div>
                <div>
                    <label class="text-xs text-muted block mb-1">Max Leverage</label>
                    <input id="riskMaxLeverage" type="number" step="1" min="1" max="125" class="w-full border border-line rounded-lg px-3 py-2.5 text-sm">
                </div>
                <div>
                    <label class="text-xs text-muted block mb-1">Max Open Positions</label>
                    <input id="riskMaxOpen" type="number" step="1" min="1" max="50" class="w-full border border-line rounded-lg px-3 py-2.5 text-sm">
                </div>
                <button type="submit" class="gold-btn text-void font-bold text-sm px-4 py-2.5 rounded-lg md:col-span-2">Save Risk Settings</button>
            </form>
        </div>
    </section>
</main>

<div id="toast" class="fixed bottom-6 right-6 z-50 hidden"></div>

<script>
    // Read from localStorage first (persists across tabs / restarts),
    // fall back to sessionStorage for users who opted out of
    // 'Remember me'. Local expiry is enforced inside _loadSession.
    let TOKEN = null;
    function _loadSession() {
        let token = localStorage.getItem('smattaker_user_token');
        let ts = localStorage.getItem('smattaker_session_ts');
        let store = localStorage;
        if (!token) {
            token = sessionStorage.getItem('smattaker_user_token');
            ts = sessionStorage.getItem('smattaker_session_ts');
            store = sessionStorage;
        }
        if (!token) return null;
        const MAX_AGE = 7 * 24 * 60 * 60 * 1000;
        if (ts && (Date.now() - parseInt(ts, 10)) > MAX_AGE) {
            store.removeItem('smattaker_user_token');
            store.removeItem('smattaker_refresh_token');
            store.removeItem('smattaker_session_ts');
            return null;
        }
        return token;
    }
    function _clearSession() {
        [localStorage, sessionStorage].forEach(s => {
            try { s.removeItem('smattaker_user_token'); } catch (e) {}
            try { s.removeItem('smattaker_refresh_token'); } catch (e) {}
            try { s.removeItem('smattaker_session_ts'); } catch (e) {}
        });
    }
    // ── Bot-issued token bootstrap ────────────────────────────
    // When the Telegram bot's /login command sends a link to
    // /dashboard?token=<JWT>#rt=<refresh>, this block saves both into
    // localStorage so the existing _loadSession() / authFetch() logic
    // picks them up automatically. The token is then stripped from the
    // URL bar so it's not visible or re-shareable after first load.
    (function _bootstrapBotToken() {
        const params = new URLSearchParams(window.location.search);
        const urlToken = params.get('token');
        if (urlToken) {
            localStorage.setItem('smattaker_user_token', urlToken);
            localStorage.setItem('smattaker_session_ts', Date.now().toString());
            // Refresh token is in the URL hash (#rt=...) so it never
            // reaches the server log. window.location.hash keeps it
            // client-side only.
            const hashParams = new URLSearchParams(
                window.location.hash.replace(/^#/, '')
            );
            const rt = hashParams.get('rt');
            if (rt) localStorage.setItem('smattaker_refresh_token', rt);
            // Strip the token from the URL bar for cleanliness + privacy.
            if (window.history && window.history.replaceState) {
                const cleanUrl = window.location.pathname + window.location.hash;
                window.history.replaceState({}, document.title, cleanUrl);
            }
        }
    })();

    TOKEN = _loadSession();
    if (!TOKEN) { window.location.href = '/login'; }

    function showToast(msg, type = 'success') {
        const el = document.getElementById('toast');
        const color = type === 'error' ? 'bg-loss' : 'bg-win';
        el.innerHTML = `<div class="${color} text-void font-bold text-sm px-5 py-3 rounded-xl shadow-lg">${msg}</div>`;
        el.classList.remove('hidden');
        setTimeout(() => el.classList.add('hidden'), 3500);
    }

    // ⚠️ Silent refresh: an expired access token used to log the user
    // out immediately on the very next API call, even though a valid
    // refresh token existed. Now it transparently exchanges the refresh
    // token for a new pair and retries the original request ONCE before
    // giving up — the user never notices their access token expired.
    let _refreshInFlight = null;
    async function tryRefreshToken() {
        if (_refreshInFlight) return _refreshInFlight;
        const refreshToken = localStorage.getItem('smattaker_refresh_token')
            || sessionStorage.getItem('smattaker_refresh_token');
        if (!refreshToken) return false;

        _refreshInFlight = (async () => {
            try {
                const res = await fetch(`/api/auth/refresh?refresh_token=${encodeURIComponent(refreshToken)}`, { method: 'POST' });
                if (!res.ok) return false;
                const json = await res.json();
                if (json.data && json.data.token) {
                    TOKEN = json.data.token;
                    const store = localStorage.getItem('smattaker_user_token') ? localStorage : sessionStorage;
                    store.setItem('smattaker_user_token', json.data.token);
                    if (json.data.refresh_token) store.setItem('smattaker_refresh_token', json.data.refresh_token);
                    store.setItem('smattaker_session_ts', Date.now().toString());
                    return true;
                }
                return false;
            } catch (e) {
                return false;
            } finally {
                _refreshInFlight = null;
            }
        })();
        return _refreshInFlight;
    }

    async function authFetch(url, options = {}, _retried = false) {
        const headers = Object.assign({}, options.headers || {}, { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' });
        const res = await fetch(url, Object.assign({}, options, { headers }));
        if (res.status === 401) {
            if (!_retried) {
                const refreshed = await tryRefreshToken();
                if (refreshed) return authFetch(url, options, true);
            }
            // Either refresh wasn't possible, or the retried request is
            // STILL unauthorized even with a fresh token — either way
            // there's no valid session left, so log out properly rather
            // than silently returning a failed response.
            _clearSession();
            window.location.href = '/login';
        }
        return res;
    }
    function logout() {
        _clearSession();
        window.location.href = '/login';
    }

    document.querySelectorAll('.nav-tab').forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));
    let _activeTab = 'overview';
    function switchTab(tab) {
        _activeTab = tab;
        document.querySelectorAll('.nav-tab').forEach(b => {
            const active = b.dataset.tab === tab;
            b.classList.toggle('text-gold', active); b.classList.toggle('border-gold', active);
            b.classList.toggle('text-slate-400', !active); b.classList.toggle('border-transparent', !active);
        });
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${tab}`));
        // Immediately reload the tab data when the user switches to it
        if (tab === 'signals') { loadSignals(); }
        else if (tab === 'trades') { loadMyTrades(); }
    }

    function statusBadge(status) {
        const map = { active: 'bg-win/15 text-win', pending_approval: 'bg-warn/15 text-warn', trial: 'bg-gold/15 text-gold',
            banned: 'bg-loss/15 text-loss', inactive: 'bg-slate-600/20 text-slate-400', completed: 'bg-win/15 text-win',
            executed: 'bg-win/15 text-win', expired: 'bg-slate-600/20 text-slate-400', cancelled: 'bg-loss/15 text-loss' };
        return `<span class="badge ${map[status] || 'bg-slate-600/20 text-slate-400'}">${(status||'').replace(/_/g,' ').toUpperCase()}</span>`;
    }

    let PROFILE = null;

    async function loadProfile() {
        const res = await authFetch('/api/account/me/full');
        const json = await res.json();
        if (!res.ok) { showToast(json.detail || 'Could not load your profile', 'error'); return; }
        PROFILE = json.data;
        const u = PROFILE.user;

        document.getElementById('userGreeting').textContent = `Welcome, ${u.full_name || u.telegram_username || 'trader'}`;

        if (u.status === 'pending_approval') {
            const banner = document.getElementById('approvalBanner');
            banner.classList.remove('hidden');
            banner.textContent = '⏳ Your account is pending admin approval. Signals and trading will unlock once approved.';
        }

        document.getElementById('accountBlock').innerHTML = `
            <div class="flex justify-between"><span class="text-muted">Status</span>${statusBadge(u.status)}</div>
            <div class="flex justify-between"><span class="text-muted">Telegram</span><span>@${u.telegram_username || '—'}</span></div>
            <div class="flex justify-between"><span class="text-muted">Language</span><span class="uppercase">${u.language}</span></div>
            <div class="flex justify-between"><span class="text-muted">Member since</span><span>${new Date(u.created_at).toLocaleDateString()}</span></div>
        `;

        const sub = PROFILE.subscriptions[0];
        const subActive = sub && ['paid', 'trial_active'].includes(sub.payment_status) && (!sub.end_date || new Date(sub.end_date) > new Date());
        document.getElementById('subscriptionBlock').innerHTML = subActive ? `
            <div class="flex items-center justify-between">
                <div>
                    <div class="text-white font-semibold capitalize">${sub.plan_type} Plan — $${sub.amount_usd}/mo</div>
                    <div class="text-xs text-muted mt-1">Renews: ${sub.end_date ? new Date(sub.end_date).toLocaleDateString() : 'Never (lifetime)'}</div>
                </div>
                ${statusBadge(sub.payment_status)}
            </div>` : `
            <div>
                <p class="mb-3">No active subscription yet.</p>
                <button onclick="openSubscribeFlow()" class="gold-btn text-void font-bold text-sm px-4 py-2.5 rounded-lg"><i class="fa-solid fa-bolt mr-2"></i>Subscribe Now</button>
            </div>
            <div id="subscribeFlow" class="hidden mt-4 pt-4 border-t border-line"></div>`;

        renderExchanges();
        renderRiskForm();
    }

    async function openSubscribeFlow() {
        const el = document.getElementById('subscribeFlow');
        el.classList.remove('hidden');
        el.innerHTML = `<p class="text-xs text-muted">Loading payment options...</p>`;

        try {
            const res = await authFetch('/api/payments/wallet-info');
            const json = await res.json();
            const w = json.data;

            if (!w.configured) {
                el.innerHTML = `<p class="text-sm text-warn">Manual crypto payment isn't configured yet. Please contact the admin directly.</p>`;
                return;
            }

            let addressHtml = '';
            w.networks.forEach(n => {
                addressHtml += `<div class="mb-2"><span class="text-xs text-muted">${n.label}:</span><div class="font-mono text-xs bg-void/50 rounded-lg p-2 mt-1 break-all">${n.address}</div></div>`;
            });
            const networkOptions = w.networks.map(n => `<option value="${n.key}">${n.label}</option>`).join('');

            el.innerHTML = `
                <p class="text-sm mb-3">Send exactly <span class="text-gold font-bold">$${w.price_usd}</span> to the matching network address below — <strong class="text-warn">sending on the wrong network can lose the funds permanently.</strong></p>
                ${addressHtml}
                <form id="txForm" class="mt-4 space-y-2">
                    <label class="text-xs text-muted block">Network you sent on</label>
                    <select id="txCurrency" class="w-full border border-line rounded-lg px-3 py-2.5 text-sm bg-void/50">
                        ${networkOptions}
                    </select>
                    <label class="text-xs text-muted block">Transaction hash (after sending)</label>
                    <input id="txHashInput" required placeholder="0x... or transaction ID" class="w-full border border-line rounded-lg px-3 py-2.5 text-sm bg-void/50">
                    <button type="submit" class="gold-btn text-void font-bold text-sm px-4 py-2.5 rounded-lg w-full">Submit for Review</button>
                </form>
            `;

            document.getElementById('txForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const payload = {
                    tx_hash: document.getElementById('txHashInput').value,
                    currency: document.getElementById('txCurrency').value,
                    amount: w.price_usd,
                };
                const vres = await authFetch('/api/payments/crypto/verify', { method: 'POST', body: JSON.stringify(payload) });
                const vjson = await vres.json();
                if (vres.ok) {
                    showToast('Submitted! An admin will review and activate your subscription shortly.');
                    el.innerHTML = `<p class="text-sm text-win">✅ Submitted for review — you'll be notified via Telegram once approved.</p>`;
                } else {
                    showToast(vjson.detail || 'Submission failed', 'error');
                }
            });
        } catch (e) {
            el.innerHTML = `<p class="text-sm text-loss">Could not load payment info. Try again shortly.</p>`;
        }
    }

    // Adaptive price formatter — small cryptos (TRX, SHIB, PEPE) need
    // many decimals; high-value assets (BTC, Gold) need few. Showing
    // everything with 2 decimals truncates $0.0334 to $0.03 which is
    // the bug the user saw.
    function fmtPrice(p) {
        if (p === null || p === undefined) return '—';
        const n = Number(p);
        if (!isFinite(n) || n === 0) return '$0.00';
        const abs = Math.abs(n);
        if (abs >= 1000) return '$' + n.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
        if (abs >= 1)      return '$' + n.toLocaleString(undefined, {minimumFractionDigits: 4, maximumFractionDigits: 4});
        if (abs >= 0.01)   return '$' + n.toFixed(6);
        return '$' + n.toFixed(8);
    }

    async function loadSignals() {
        const res = await authFetch('/api/signals/active');
        const json = await res.json();
        const list = document.getElementById('signalsList');
        const upd = document.getElementById('signalsUpdated');
        if (!res.ok) { list.innerHTML = `<p class="text-muted text-sm col-span-2 text-center py-8">${json.detail || 'Could not load signals.'}</p>`; if (upd) upd.textContent = ''; return; }
        if (upd) { const now = new Date(); upd.textContent = '· updated ' + now.toLocaleTimeString(); }
        const items = json.data || [];
        list.innerHTML = items.length ? items.map(s => `
            <div class="bg-card border border-line rounded-2xl p-5">
                <div class="flex items-center justify-between mb-3">
                    <span class="font-bold text-white">${s.symbol}</span>
                    <span class="badge ${s.direction === 'long' ? 'bg-win/15 text-win' : 'bg-loss/15 text-loss'}">${s.direction.toUpperCase()}</span>
                </div>
                <div class="grid grid-cols-3 gap-2 text-xs">
                    <div><div class="text-muted">Entry</div><div class="font-semibold">${fmtPrice(s.entry_price)}</div></div>
                    <div><div class="text-muted">Stop Loss</div><div class="font-semibold text-loss">${fmtPrice(s.stop_loss)}</div></div>
                    <div><div class="text-muted">Take Profit</div><div class="font-semibold text-win">${(s.take_profit_levels && s.take_profit_levels[0]) ? s.take_profit_levels[0].price : '—'}</div></div>
                </div>
                <div class="text-xs text-muted mt-3">Confidence: ${(s.confidence_score||0).toFixed(1)}% · ${s.strategy_type}</div>
            </div>`).join('') : `<p class="text-muted text-sm col-span-2 text-center py-8">No active signals right now — check back soon.</p>`;
    }

    async function loadMyTrades() {
        const res = await authFetch('/api/trades/');
        const json = await res.json();
        const body = document.getElementById('myTradesBody');
        if (!res.ok) { body.innerHTML = `<tr><td colspan="6" class="text-center text-muted py-6">${json.detail || 'Could not load trades.'}</td></tr>`; return; }
        const items = (json.data && json.data.trades) || [];
        body.innerHTML = items.length ? items.map(t => `
            <tr class="border-t border-line">
                <td class="px-4 py-3 font-semibold">${t.symbol}</td>
                <td class="px-4 py-3"><span class="badge ${t.direction === 'long' ? 'bg-win/15 text-win' : 'bg-loss/15 text-loss'}">${t.direction.toUpperCase()}</span></td>
                <td class="px-4 py-3 text-right">${t.entry_price}</td>
                <td class="px-4 py-3 text-right">${t.exit_price ?? '—'}</td>
                <td class="px-4 py-3 text-right ${(t.pnl_percent??0)>=0?'text-win':'text-loss'}">${t.pnl_percent!=null ? t.pnl_percent.toFixed(2)+'%' : '—'}</td>
                <td class="px-4 py-3 text-center">${statusBadge(t.status)}</td>
            </tr>`).join('') : `<tr><td colspan="6" class="text-center text-muted py-6">You have no trades yet.</td></tr>`;
    }

    function renderExchanges() {
        const list = document.getElementById('exchangesList');
        const items = PROFILE.exchange_connections || [];
        list.innerHTML = items.length ? items.map(e => `
            <div class="bg-card border border-line rounded-xl p-4 flex items-center justify-between">
                <div>
                    <div class="font-semibold text-white capitalize">${e.exchange_label || e.exchange_name} <span class="text-xs text-muted">(${e.exchange_name})</span></div>
                    <div class="text-xs text-muted mt-1">Key: ${e.api_key_preview} ${e.is_testnet ? '· Testnet' : ''}</div>
                </div>
                <div class="flex items-center gap-2">
                    <span class="badge ${e.connection_status === 'ok' ? 'bg-win/15 text-win' : e.connection_status === 'error' ? 'bg-loss/15 text-loss' : 'bg-slate-600/20 text-slate-400'}">${e.connection_status.toUpperCase()}</span>
                    <span class="badge ${e.is_active ? 'bg-gold/15 text-gold' : 'bg-slate-600/20 text-slate-400'}">${e.is_active ? 'ENABLED' : 'DISABLED'}</span>
                    <button onclick="toggleExchange('${e.id}')" class="w-8 h-8 rounded-lg bg-void/60 hover:bg-void text-slate-300 transition"><i class="fa-solid fa-power-off text-xs"></i></button>
                    <button onclick="deleteExchange('${e.id}')" class="w-8 h-8 rounded-lg bg-loss/10 hover:bg-loss/20 text-loss transition"><i class="fa-solid fa-trash text-xs"></i></button>
                </div>
            </div>`).join('') : `<p class="text-muted text-sm text-center py-6">No exchanges connected yet.</p>`;
    }

    document.getElementById('exchangeForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const payload = {
            exchange_name: document.getElementById('exExchange').value,
            exchange_label: document.getElementById('exLabel').value || null,
            api_key: document.getElementById('exApiKey').value,
            secret_key: document.getElementById('exSecretKey').value,
            passphrase: document.getElementById('exPassphrase').value || null,
            is_testnet: document.getElementById('exTestnet').checked,
        };
        const res = await authFetch('/api/account/exchange', { method: 'POST', body: JSON.stringify(payload) });
        const json = await res.json();
        if (res.ok) { showToast(json.message || 'Exchange connected'); e.target.reset(); loadProfile(); }
        else showToast(json.detail || 'Failed to connect exchange', 'error');
    });

    async function toggleExchange(id) {
        const res = await authFetch(`/api/account/exchange/${id}/toggle`, { method: 'PUT' });
        if (res.ok) { showToast('Exchange updated'); loadProfile(); } else showToast('Failed', 'error');
    }
    async function deleteExchange(id) {
        if (!confirm('Remove this exchange connection permanently?')) return;
        const res = await authFetch(`/api/account/exchange/${id}`, { method: 'DELETE' });
        if (res.ok) { showToast('Exchange removed'); loadProfile(); } else showToast('Failed', 'error');
    }

    function renderRiskForm() {
        const risk = (PROFILE.risk_settings || []).find(r => r.account_type === document.getElementById('riskAccountType').value) || PROFILE.risk_settings[0];
        if (!risk) return;
        document.getElementById('riskAccountType').value = risk.account_type;
        document.getElementById('riskSizingMethod').value = risk.position_sizing_method;
        document.getElementById('riskMaxPerTrade').value = risk.max_risk_per_trade_pct;
        document.getElementById('riskFixedSize').value = risk.fixed_position_size;
        document.getElementById('riskMaxLeverage').value = risk.max_leverage;
        document.getElementById('riskMaxOpen').value = risk.max_open_positions;
    }
    document.getElementById('riskAccountType').addEventListener('change', renderRiskForm);

    document.getElementById('riskForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const payload = {
            account_type: document.getElementById('riskAccountType').value,
            position_sizing_method: document.getElementById('riskSizingMethod').value,
            max_risk_per_trade_pct: parseFloat(document.getElementById('riskMaxPerTrade').value),
            fixed_position_size: parseFloat(document.getElementById('riskFixedSize').value),
            max_leverage: parseInt(document.getElementById('riskMaxLeverage').value),
            max_open_positions: parseInt(document.getElementById('riskMaxOpen').value),
        };
        const res = await authFetch('/api/account/risk', { method: 'PUT', body: JSON.stringify(payload) });
        const json = await res.json();
        if (res.ok) { showToast('Risk settings saved'); loadProfile(); } else showToast(json.detail || 'Failed to save', 'error');
    });

    // Boot
    loadProfile();
    loadSignals();
    loadMyTrades();

    // ── Auto-refresh ──────────────────────────────────────────
    // Signals are regenerated by the scheduler every 15 minutes,
    // so refreshing every 60 seconds is plenty for the user to
    // see new entries without hammering the API.  We only fire
    // when the Signals (or Overview) tab is visible so background
    // tabs don't waste bandwidth.  Also refresh trades when that
    // tab is active.
    let _signalsTimer = setInterval(() => {
        if (_activeTab === 'signals' || _activeTab === 'overview') {
            loadSignals();
        }
        if (_activeTab === 'trades') {
            loadMyTrades();
        }
    }, 60000);
    // Stop the timer if the page is hidden for a long time to save resources
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            clearInterval(_signalsTimer);
            _signalsTimer = null;
        } else if (!_signalsTimer) {
            // Resume when the user comes back — and refresh immediately
            loadSignals();
            if (_activeTab === 'trades') { loadMyTrades(); }
            _signalsTimer = setInterval(() => {
                if (_activeTab === 'signals' || _activeTab === 'overview') { loadSignals(); }
                if (_activeTab === 'trades') { loadMyTrades(); }
            }, 60000);
        }
    });
</script>
</body>
</html>

"""
    return HTMLResponse(content=html_content)


@app.get("/admin", response_class=HTMLResponse, tags=["system"])
async def admin_panel():
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SmAttaker — Command Center</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    fontFamily: { sans: ['Manrope', 'sans-serif'], display: ['Space Grotesk', 'sans-serif'] },
                    colors: {
                        void: '#05070D',
                        panel: '#0B0F1A',
                        card: '#10162A',
                        line: '#1E2740',
                        gold: { DEFAULT: '#D4AF37', light: '#F0D683', dark: '#9C7A1E' },
                        win: '#22C55E',
                        loss: '#EF4444',
                        warn: '#F59E0B',
                        muted: '#8B93A8',
                    },
                    boxShadow: {
                        gold: '0 0 0 1px rgba(212,175,55,0.15), 0 8px 24px -8px rgba(212,175,55,0.25)',
                        card: '0 1px 0 rgba(255,255,255,0.03) inset, 0 10px 30px -12px rgba(0,0,0,0.6)',
                    },
                }
            }
        }
    </script>
    <style>
        body { background: radial-gradient(1200px 600px at 15% -10%, rgba(212,175,55,0.08), transparent), #05070D; }
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: #0B0F1A; }
        ::-webkit-scrollbar-thumb { background: #1E2740; border-radius: 8px; }
        .gold-text { background: linear-gradient(135deg, #F0D683 0%, #D4AF37 45%, #9C7A1E 100%); -webkit-background-clip: text; background-clip: text; color: transparent; }
        .gold-btn { background: linear-gradient(135deg, #F0D683 0%, #D4AF37 60%, #C9A227 100%); }
        .nav-item.active { background: linear-gradient(90deg, rgba(212,175,55,0.14), rgba(212,175,55,0.02)); border-left: 2px solid #D4AF37; color: #F0D683; }
        .nav-item:not(.active):hover { background: rgba(255,255,255,0.03); }
        .tab-panel { display: none; }
        .tab-panel.active { display: block; animation: fadeIn .25s ease; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(4px);} to { opacity: 1; transform: translateY(0);} }
        .skeleton { background: linear-gradient(90deg, #10162A 25%, #161d33 37%, #10162A 63%); background-size: 400% 100%; animation: shimmer 1.4s ease infinite; }
        @keyframes shimmer { 0% { background-position: 100% 50%; } 100% { background-position: 0 50%; } }
        .badge { font-size: 11px; font-weight: 700; letter-spacing: .03em; padding: 2px 8px; border-radius: 999px; }
    </style>
</head>
<body class="text-slate-200 font-sans min-h-screen">

<div class="flex min-h-screen">

    <!-- ══════════ SIDEBAR ══════════ -->
    <aside class="w-64 shrink-0 bg-panel border-r border-line flex flex-col fixed h-screen z-30">
        <div class="px-5 py-6 border-b border-line">
            <div class="flex items-center gap-3">
                <div class="w-10 h-10 rounded-xl gold-btn flex items-center justify-center shadow-gold shrink-0">
                    <i class="fa-solid fa-kaaba text-void text-lg"></i>
                </div>
                <div>
                    <div class="font-display font-bold text-lg leading-tight gold-text">SMATTAKER</div>
                    <div class="text-[10px] uppercase tracking-widest text-muted">Command Center</div>
                </div>
            </div>
        </div>

        <nav class="flex-1 overflow-y-auto py-4 px-2 space-y-1" id="navMenu">
            <div class="px-3 text-[10px] uppercase tracking-widest text-muted font-bold mb-2 mt-1">Overview</div>
            <button class="nav-item active w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition" data-tab="overview">
                <i class="fa-solid fa-gauge-high w-4 text-center"></i> Dashboard
            </button>
            <button class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition text-slate-300" data-tab="analytics">
                <i class="fa-solid fa-chart-pie w-4 text-center"></i> Analytics
            </button>

            <div class="px-3 text-[10px] uppercase tracking-widest text-muted font-bold mb-2 mt-5">Operations</div>
            <button class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition text-slate-300" data-tab="signals">
                <i class="fa-solid fa-bolt w-4 text-center"></i> Signals
                <span id="navSignalsBadge" class="ml-auto text-[10px] bg-gold/15 text-gold px-1.5 py-0.5 rounded-full font-bold"></span>
            </button>
            <button class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition text-slate-300" data-tab="trades">
                <i class="fa-solid fa-scale-balanced w-4 text-center"></i> Trade Journal
            </button>
            <button class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition text-slate-300" data-tab="rankings">
                <i class="fa-solid fa-trophy w-4 text-center"></i> Instrument Rankings
            </button>

            <div class="px-3 text-[10px] uppercase tracking-widest text-muted font-bold mb-2 mt-5">Business</div>
            <button class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition text-slate-300" data-tab="users">
                <i class="fa-solid fa-users w-4 text-center"></i> Users
            </button>
            <button class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition text-slate-300" data-tab="payments">
                <i class="fa-solid fa-coins w-4 text-center"></i> Payments
                <span id="navPaymentsBadge" class="ml-auto text-[10px] bg-warn/15 text-warn px-1.5 py-0.5 rounded-full font-bold"></span>
            </button>

            <div class="px-3 text-[10px] uppercase tracking-widest text-muted font-bold mb-2 mt-5">Platform</div>
            <button class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition text-slate-300" data-tab="audit">
                <i class="fa-solid fa-clipboard-list w-4 text-center"></i> Audit Log
            </button>
            <button class="nav-item w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-semibold transition text-slate-300" data-tab="system">
                <i class="fa-solid fa-server w-4 text-center"></i> System &amp; Engines
            </button>
        </nav>

        <div class="p-4 border-t border-line">
            <button onclick="resetAdminToken()" class="w-full flex items-center gap-2 justify-center text-xs text-muted hover:text-slate-300 transition py-2">
                <i class="fa-solid fa-key"></i> Reset Admin Session
            </button>
        </div>
    </aside>

    <!-- ══════════ MAIN ══════════ -->
    <div class="flex-1 ml-64">
        <!-- Topbar -->
        <header class="sticky top-0 z-20 bg-void/80 backdrop-blur border-b border-line px-8 py-4 flex items-center justify-between">
            <div>
                <h1 id="pageTitle" class="font-display font-bold text-xl text-white">Dashboard</h1>
                <p id="pageSubtitle" class="text-xs text-muted mt-0.5">Real-time institutional performance overview</p>
            </div>
            <div class="flex items-center gap-3">
                <button onclick="triggerStrategies()" class="gold-btn text-void font-bold text-sm px-4 py-2.5 rounded-xl shadow-gold hover:brightness-110 transition flex items-center gap-2">
                    <i class="fa-solid fa-bolt"></i> Trigger Strategies
                </button>
                <div class="flex items-center gap-2 bg-card border border-line rounded-xl px-3 py-2.5">
                    <span class="w-2 h-2 rounded-full bg-win animate-pulse"></span>
                    <span class="text-xs font-bold text-win">SYSTEM LIVE</span>
                </div>
            </div>
        </header>

        <main class="p-8 space-y-8">

            <!-- ═══ OVERVIEW TAB ═══ -->
            <section id="tab-overview" class="tab-panel active space-y-6">
                <div class="grid grid-cols-2 lg:grid-cols-4 gap-5" id="kpiCards"></div>

                <div class="grid grid-cols-1 lg:grid-cols-3 gap-5">
                    <div class="lg:col-span-2 bg-card border border-line rounded-2xl p-6 shadow-card">
                        <div class="flex items-center justify-between mb-1">
                            <div>
                                <h3 class="font-display font-bold text-lg text-white">Equity Curve</h3>
                                <p class="text-xs text-muted">Cumulative performance across all completed trades</p>
                            </div>
                            <i class="fa-solid fa-chart-area text-gold text-xl"></i>
                        </div>
                        <div class="h-72 mt-4"><canvas id="equityChart"></canvas></div>
                    </div>

                    <div class="bg-card border border-line rounded-2xl p-6 shadow-card">
                        <div class="flex items-center justify-between mb-4">
                            <h3 class="font-display font-bold text-lg text-white">Streak &amp; Risk</h3>
                            <i class="fa-solid fa-fire text-gold text-xl"></i>
                        </div>
                        <div class="space-y-4" id="streakBlock"></div>
                    </div>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
                    <div class="bg-card border border-line rounded-2xl p-6 shadow-card">
                        <div class="flex items-center justify-between mb-4">
                            <h3 class="font-display font-bold text-lg text-white">Monthly R-Heatmap</h3>
                            <i class="fa-solid fa-border-all text-gold text-xl"></i>
                        </div>
                        <div id="heatmapGrid" class="grid grid-cols-4 gap-2"></div>
                    </div>
                    <div class="bg-card border border-line rounded-2xl p-6 shadow-card">
                        <div class="flex items-center justify-between mb-4">
                            <h3 class="font-display font-bold text-lg text-white">Live Signal Feed</h3>
                            <i class="fa-solid fa-satellite-dish text-gold text-xl"></i>
                        </div>
                        <div id="miniSignalFeed" class="space-y-2 max-h-72 overflow-y-auto"></div>
                    </div>
                </div>
            </section>

            <!-- ═══ ANALYTICS TAB ═══ -->
            <section id="tab-analytics" class="tab-panel space-y-6">
                <div class="grid grid-cols-2 lg:grid-cols-4 gap-5" id="analyticsKpis"></div>
                <div class="bg-card border border-line rounded-2xl p-6 shadow-card">
                    <h3 class="font-display font-bold text-lg text-white mb-4">Monthly Return Distribution</h3>
                    <div class="h-72"><canvas id="monthlyChart"></canvas></div>
                </div>
            </section>

            <!-- ═══ SIGNALS TAB ═══ -->
            <section id="tab-signals" class="tab-panel space-y-6">
                <div class="flex items-center justify-between">
                    <div class="flex gap-2" id="signalFilters">
                        <button class="filter-chip active px-4 py-2 rounded-lg text-xs font-bold bg-gold/15 text-gold border border-gold/30" data-status="">All</button>
                        <button class="filter-chip px-4 py-2 rounded-lg text-xs font-bold bg-card border border-line text-slate-300" data-status="active">Active</button>
                        <button class="filter-chip px-4 py-2 rounded-lg text-xs font-bold bg-card border border-line text-slate-300" data-status="executed">Executed</button>
                        <button class="filter-chip px-4 py-2 rounded-lg text-xs font-bold bg-card border border-line text-slate-300" data-status="expired">Expired</button>
                        <button class="filter-chip px-4 py-2 rounded-lg text-xs font-bold bg-card border border-line text-slate-300" data-status="cancelled">Cancelled</button>
                    </div>
                </div>
                <div class="bg-card border border-line rounded-2xl shadow-card overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="w-full text-sm">
                            <thead class="bg-void/60 text-muted text-xs uppercase tracking-wider">
                                <tr>
                                    <th class="text-left px-5 py-3">Symbol</th>
                                    <th class="text-left px-5 py-3">Direction</th>
                                    <th class="text-left px-5 py-3">Strategy</th>
                                    <th class="text-right px-5 py-3">Entry</th>
                                    <th class="text-right px-5 py-3">Stop Loss</th>
                                    <th class="text-right px-5 py-3">Take Profit</th>
                                    <th class="text-right px-5 py-3">Confidence</th>
                                    <th class="text-center px-5 py-3">Status</th>
                                </tr>
                            </thead>
                            <tbody id="signalsTableBody"></tbody>
                        </table>
                    </div>
                </div>
            </section>

            <!-- ═══ TRADES TAB ═══ -->
            <section id="tab-trades" class="tab-panel space-y-6">
                <div class="bg-card border border-line rounded-2xl shadow-card overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="w-full text-sm">
                            <thead class="bg-void/60 text-muted text-xs uppercase tracking-wider">
                                <tr>
                                    <th class="text-left px-5 py-3">Symbol</th>
                                    <th class="text-left px-5 py-3">Account</th>
                                    <th class="text-left px-5 py-3">Direction</th>
                                    <th class="text-right px-5 py-3">Entry</th>
                                    <th class="text-right px-5 py-3">Exit</th>
                                    <th class="text-right px-5 py-3">P&amp;L %</th>
                                    <th class="text-right px-5 py-3">R-Multiple</th>
                                    <th class="text-center px-5 py-3">Status</th>
                                </tr>
                            </thead>
                            <tbody id="tradesTableBody"></tbody>
                        </table>
                    </div>
                </div>
            </section>

            <!-- ═══ RANKINGS TAB ═══ -->
            <section id="tab-rankings" class="tab-panel space-y-6">
                <div class="bg-card border border-line rounded-2xl shadow-card overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="w-full text-sm">
                            <thead class="bg-void/60 text-muted text-xs uppercase tracking-wider">
                                <tr>
                                    <th class="text-left px-5 py-3">#</th>
                                    <th class="text-left px-5 py-3">Symbol</th>
                                    <th class="text-left px-5 py-3">Asset Class</th>
                                    <th class="text-right px-5 py-3">Trades</th>
                                    <th class="text-right px-5 py-3">Win Rate</th>
                                    <th class="text-right px-5 py-3">Profit Factor</th>
                                    <th class="text-right px-5 py-3">Avg R</th>
                                    <th class="text-right px-5 py-3">Best / Worst</th>
                                </tr>
                            </thead>
                            <tbody id="rankingsTableBody"></tbody>
                        </table>
                    </div>
                </div>
            </section>

            <!-- ═══ USERS TAB ═══ -->
            <section id="tab-users" class="tab-panel space-y-6">
                <div class="flex items-center gap-3">
                    <div class="relative flex-1 max-w-sm">
                        <i class="fa-solid fa-magnifying-glass absolute left-3.5 top-1/2 -translate-y-1/2 text-muted text-xs"></i>
                        <input id="userSearch" placeholder="Search users..." class="w-full bg-card border border-line rounded-xl pl-9 pr-3 py-2.5 text-sm focus:outline-none focus:border-gold/50 transition">
                    </div>
                    <select id="userStatusFilter" class="bg-card border border-line rounded-xl px-3 py-2.5 text-sm focus:outline-none focus:border-gold/50">
                        <option value="">All Statuses</option>
                        <option value="active">Active</option>
                        <option value="pending_approval">Pending</option>
                        <option value="trial">Trial</option>
                        <option value="banned">Banned</option>
                        <option value="inactive">Inactive</option>
                    </select>
                    <button onclick="fetchUsers()" class="bg-card border border-line rounded-xl px-4 py-2.5 text-sm font-semibold hover:border-gold/40 transition">
                        <i class="fa-solid fa-rotate"></i>
                    </button>
                </div>
                <div class="bg-card border border-line rounded-2xl shadow-card overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="w-full text-sm">
                            <thead class="bg-void/60 text-muted text-xs uppercase tracking-wider">
                                <tr>
                                    <th class="text-left px-5 py-3">User</th>
                                    <th class="text-left px-5 py-3">Telegram ID</th>
                                    <th class="text-left px-5 py-3">Role</th>
                                    <th class="text-left px-5 py-3">Status</th>
                                    <th class="text-left px-5 py-3">Joined</th>
                                    <th class="text-center px-5 py-3">Actions</th>
                                </tr>
                            </thead>
                            <tbody id="usersTableBody"></tbody>
                        </table>
                    </div>
                </div>
            </section>

            <!-- ═══ PAYMENTS TAB ═══ -->
            <section id="tab-payments" class="tab-panel space-y-6">
                <div class="bg-card border border-line rounded-2xl p-5 shadow-card flex items-start gap-3">
                    <i class="fa-solid fa-circle-info text-gold mt-0.5"></i>
                    <p class="text-sm text-muted">Manual crypto payment confirmations awaiting review appear here. Approving activates the subscription immediately; rejecting notifies the user.</p>
                </div>
                <div class="bg-card border border-line rounded-2xl shadow-card overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="w-full text-sm">
                            <thead class="bg-void/60 text-muted text-xs uppercase tracking-wider">
                                <tr>
                                    <th class="text-left px-5 py-3">User</th>
                                    <th class="text-left px-5 py-3">Plan</th>
                                    <th class="text-right px-5 py-3">Amount</th>
                                    <th class="text-left px-5 py-3">TX Hash</th>
                                    <th class="text-left px-5 py-3">Submitted</th>
                                    <th class="text-center px-5 py-3">Actions</th>
                                </tr>
                            </thead>
                            <tbody id="paymentsTableBody"></tbody>
                        </table>
                    </div>
                </div>
            </section>

            <!-- ═══ AUDIT LOG TAB ═══ -->
            <section id="tab-audit" class="tab-panel space-y-6">
                <div class="bg-card border border-line rounded-2xl p-5 shadow-card flex items-start gap-3">
                    <i class="fa-solid fa-shield-halved text-gold mt-0.5"></i>
                    <p class="text-sm text-muted">Every sensitive admin action — status changes, payment decisions, trial approvals — is recorded here permanently for accountability.</p>
                </div>
                <div class="bg-card border border-line rounded-2xl shadow-card overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="w-full text-sm">
                            <thead class="bg-void/60 text-muted text-xs uppercase tracking-wider">
                                <tr>
                                    <th class="text-left px-5 py-3">When</th>
                                    <th class="text-left px-5 py-3">Admin</th>
                                    <th class="text-left px-5 py-3">Action</th>
                                    <th class="text-left px-5 py-3">Target</th>
                                    <th class="text-left px-5 py-3">Details</th>
                                </tr>
                            </thead>
                            <tbody id="auditLogTableBody"></tbody>
                        </table>
                    </div>
                </div>
            </section>

            <!-- ═══ SYSTEM TAB ═══ -->
            <section id="tab-system" class="tab-panel space-y-6">
                <div class="grid grid-cols-1 md:grid-cols-3 gap-5">
                    <div class="bg-card border border-line rounded-2xl p-6 shadow-card">
                        <div class="flex items-center gap-3 mb-3">
                            <div class="w-10 h-10 rounded-xl bg-win/10 flex items-center justify-center"><i class="fa-solid fa-clock-rotate-left text-win"></i></div>
                            <h3 class="font-bold text-white">Strategy Scheduler</h3>
                        </div>
                        <p class="text-xs text-muted leading-relaxed">Runs automatically via APScheduler every <span class="text-gold font-bold" id="schedulerInterval">—</span> minutes. Manual trigger available anytime from the topbar.</p>
                    </div>
                    <div class="bg-card border border-line rounded-2xl p-6 shadow-card">
                        <div class="flex items-center gap-3 mb-3">
                            <div class="w-10 h-10 rounded-xl bg-gold/10 flex items-center justify-center"><i class="fa-solid fa-robot text-gold"></i></div>
                            <h3 class="font-bold text-white">Strategy Engines</h3>
                        </div>
                        <p class="text-xs text-muted leading-relaxed">Singularity v40 (crypto) &amp; Aurum v2 (gold/forex) — both report a single validated take-profit/stop-loss barrier per signal, matching their backtests exactly.</p>
                    </div>
                    <div class="bg-card border border-line rounded-2xl p-6 shadow-card">
                        <div class="flex items-center gap-3 mb-3">
                            <div class="w-10 h-10 rounded-xl bg-gold/10 flex items-center justify-center"><i class="fa-solid fa-shield-halved text-gold"></i></div>
                            <h3 class="font-bold text-white">Admin Session</h3>
                        </div>
                        <p class="text-xs text-muted leading-relaxed">This panel requires a valid admin JWT for every write action. Session token is kept in this tab only (sessionStorage) — never persisted to disk.</p>
                    </div>
                </div>

                <div class="bg-card border border-line rounded-2xl p-6 shadow-card">
                    <div class="flex items-center gap-3 mb-3">
                        <div class="w-10 h-10 rounded-xl bg-gold/10 flex items-center justify-center"><i class="fa-solid fa-bullhorn text-gold"></i></div>
                        <h3 class="font-bold text-white">Broadcast Message</h3>
                    </div>
                    <p class="text-xs text-muted mb-3">Sends a Telegram message to every active/trial user. Rate-limited to 3 per hour — this is irreversible once sent, double-check before confirming.</p>
                    <textarea id="broadcastText" rows="3" placeholder="Your announcement..." class="w-full bg-void/50 border border-line rounded-lg px-3 py-2.5 text-sm mb-3"></textarea>
                    <button onclick="sendBroadcast()" class="gold-btn text-void font-bold text-sm px-4 py-2.5 rounded-lg"><i class="fa-solid fa-paper-plane mr-2"></i>Send Broadcast</button>
                </div>
            </section>

        </main>
    </div>
</div>

<!-- Toast -->
<div id="toast" class="fixed bottom-6 right-6 z-50 hidden"></div>

<!-- User Detail Modal -->
<div id="userDetailModal" class="hidden fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="bg-panel border border-line rounded-2xl max-w-2xl w-full max-h-[85vh] overflow-y-auto">
        <div class="flex items-center justify-between p-5 border-b border-line sticky top-0 bg-panel">
            <h3 class="font-display font-bold text-lg text-white">User Detail</h3>
            <button onclick="closeUserDetail()" class="w-8 h-8 rounded-lg bg-void/60 hover:bg-void text-slate-300 transition"><i class="fa-solid fa-xmark"></i></button>
        </div>
        <div id="userDetailBody" class="p-5"></div>
    </div>
</div>

<script>
    // ══════════════════════════════════════════════════════
    // Admin session token
    // ══════════════════════════════════════════════════════
    let ADMIN_TOKEN = sessionStorage.getItem('smattaker_admin_token') || '';

    function ensureAdminToken() {
        if (!ADMIN_TOKEN) {
            const entered = prompt('Enter your admin JWT (Authorization Bearer token):');
            if (entered) {
                ADMIN_TOKEN = entered.trim();
                sessionStorage.setItem('smattaker_admin_token', ADMIN_TOKEN);
            }
        }
        return ADMIN_TOKEN;
    }
    function resetAdminToken() {
        sessionStorage.removeItem('smattaker_admin_token');
        ADMIN_TOKEN = '';
        ensureAdminToken();
    }
    async function authFetch(url, options = {}) {
        ensureAdminToken();
        const headers = Object.assign({}, options.headers || {}, { 'Authorization': 'Bearer ' + ADMIN_TOKEN });
        const response = await fetch(url, Object.assign({}, options, { headers }));
        if (response.status === 401 || response.status === 403) {
            sessionStorage.removeItem('smattaker_admin_token');
            ADMIN_TOKEN = '';
            showToast('Admin session invalid or expired. Please re-enter your token.', 'error');
        }
        return response;
    }
    function showToast(msg, type = 'success') {
        const el = document.getElementById('toast');
        const color = type === 'error' ? 'bg-loss' : type === 'warn' ? 'bg-warn' : 'bg-win';
        el.innerHTML = `<div class="${color} text-void font-bold text-sm px-5 py-3 rounded-xl shadow-lg">${msg}</div>`;
        el.classList.remove('hidden');
        setTimeout(() => el.classList.add('hidden'), 3500);
    }

    // ══════════════════════════════════════════════════════
    // Navigation
    // ══════════════════════════════════════════════════════
    const pageMeta = {
        overview:  ['Dashboard', 'Real-time institutional performance overview'],
        analytics: ['Analytics', 'Sharpe, Sortino, drawdown & monthly performance breakdown'],
        signals:   ['Signals', 'Live and historical signals generated by the strategy engines'],
        trades:    ['Trade Journal', 'Complete record of executed trades across all users'],
        rankings:  ['Instrument Rankings', 'Performance ranked by symbol'],
        users:     ['Users', 'Manage subscriber accounts and access'],
        payments:  ['Payments', 'Review and confirm manual crypto payments'],
        audit:     ['Audit Log', 'Full history of sensitive admin actions'],
        system:    ['System & Engines', 'Scheduler, strategy engines and session status'],
    };
    document.querySelectorAll('.nav-item').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });
    function switchTab(tab) {
        document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${tab}`));
        document.getElementById('pageTitle').textContent = pageMeta[tab][0];
        document.getElementById('pageSubtitle').textContent = pageMeta[tab][1];
        const loaders = { overview: fetchOverview, analytics: fetchOverview, signals: fetchSignals, trades: fetchTrades, rankings: fetchOverview, users: fetchUsers, payments: fetchPayments, audit: fetchAuditLog, system: () => {} };
        if (loaders[tab]) loaders[tab]();
    }

    // ══════════════════════════════════════════════════════
    // Overview / Analytics
    // ══════════════════════════════════════════════════════
    let equityChartInstance = null, monthlyChartInstance = null;

    function kpiCard(label, value, icon, colorClass = 'text-gold') {
        return `
        <div class="bg-card border border-line rounded-2xl p-5 shadow-card">
            <div class="flex items-start justify-between">
                <div>
                    <p class="text-[11px] uppercase tracking-wider text-muted font-bold">${label}</p>
                    <p class="text-2xl font-display font-extrabold mt-2 ${colorClass}">${value}</p>
                </div>
                <div class="w-9 h-9 rounded-xl bg-void/60 flex items-center justify-center">
                    <i class="fa-solid ${icon} text-sm ${colorClass}"></i>
                </div>
            </div>
        </div>`;
    }

    async function fetchOverview() {
        try {
            const res = await authFetch('/api/analytics/dashboard');
            const json = await res.json();
            const d = json.data;
            if (!d) return;
            const s = d.summary || {};

            document.getElementById('kpiCards').innerHTML = [
                kpiCard('Win Rate', `${(s.win_rate ?? 0).toFixed(1)}%`, 'fa-trophy'),
                kpiCard('Profit Factor', `${(s.profit_factor ?? 0).toFixed(2)}`, 'fa-coins'),
                kpiCard('Total Return', `${(s.total_return ?? 0) >= 0 ? '+' : ''}${(s.total_return ?? 0).toFixed(2)}%`, 'fa-percent', (s.total_return ?? 0) >= 0 ? 'text-win' : 'text-loss'),
                kpiCard('Sharpe Ratio', `${(s.sharpe_ratio ?? 0).toFixed(2)}`, 'fa-scale-balanced'),
            ].join('');

            document.getElementById('analyticsKpis').innerHTML = [
                kpiCard('Sortino Ratio', `${(s.sortino_ratio ?? 0).toFixed(2)}`, 'fa-water'),
                kpiCard('Max Drawdown', `${(s.max_drawdown_pct ?? 0).toFixed(2)}%`, 'fa-arrow-trend-down', 'text-loss'),
                kpiCard('Total Trades', `${s.total_trades ?? 0}`, 'fa-list-check'),
                kpiCard('Avg R-Multiple', `${(s.average_r ?? 0).toFixed(2)}R`, 'fa-bullseye'),
            ].join('');

            document.getElementById('streakBlock').innerHTML = `
                <div class="flex justify-between items-center"><span class="text-xs text-muted">Current Streak</span><span class="font-bold ${s.current_streak_type === 'win' ? 'text-win' : 'text-loss'}">${s.current_streak ?? 0} ${s.current_streak_type || '—'}</span></div>
                <div class="flex justify-between items-center"><span class="text-xs text-muted">Best Win Streak</span><span class="font-bold text-win">${s.max_win_streak ?? 0}</span></div>
                <div class="flex justify-between items-center"><span class="text-xs text-muted">Worst Loss Streak</span><span class="font-bold text-loss">${s.max_loss_streak ?? 0}</span></div>
                <div class="h-px bg-line my-2"></div>
                <div class="flex justify-between items-center"><span class="text-xs text-muted">Avg Win</span><span class="font-bold text-win">+${(s.avg_win_pct ?? 0).toFixed(2)}%</span></div>
                <div class="flex justify-between items-center"><span class="text-xs text-muted">Avg Loss</span><span class="font-bold text-loss">${(s.avg_loss_pct ?? 0).toFixed(2)}%</span></div>
                <div class="flex justify-between items-center"><span class="text-xs text-muted">Best Trade</span><span class="font-bold text-win">+${(s.best_trade_pct ?? 0).toFixed(2)}%</span></div>
                <div class="flex justify-between items-center"><span class="text-xs text-muted">Worst Trade</span><span class="font-bold text-loss">${(s.worst_trade_pct ?? 0).toFixed(2)}%</span></div>
            `;

            renderEquityChart(d.equity_curve || []);
            renderHeatmap(d.r_heatmap);
            renderMonthlyChart(d.r_heatmap);
            renderRankingsTable(d.top_instruments || []);
        } catch (e) { console.error(e); }
    }

    function renderEquityChart(curve) {
        const ctx = document.getElementById('equityChart').getContext('2d');
        const labels = curve.map(p => new Date(p.date).toLocaleDateString());
        const values = curve.map(p => p.equity);
        if (equityChartInstance) equityChartInstance.destroy();
        equityChartInstance = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets: [{
                data: values, borderColor: '#D4AF37', borderWidth: 2, pointRadius: 0, tension: 0.35,
                fill: true, backgroundColor: (c) => {
                    const g = c.chart.ctx.createLinearGradient(0, 0, 0, 280);
                    g.addColorStop(0, 'rgba(212,175,55,0.25)'); g.addColorStop(1, 'rgba(212,175,55,0)');
                    return g;
                }
            }]},
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    y: { grid: { color: 'rgba(30,39,64,0.6)' }, ticks: { color: '#8B93A8', font: { size: 11 } } },
                    x: { grid: { display: false }, ticks: { color: '#8B93A8', font: { size: 10 }, maxTicksLimit: 8 } }
                }
            }
        });
    }

    function renderHeatmap(heatmap) {
        const grid = document.getElementById('heatmapGrid');
        if (!heatmap || !heatmap.cells || !heatmap.cells.length) {
            grid.innerHTML = `<p class="text-xs text-muted col-span-4 text-center py-6">No completed trades yet — data will populate as signals resolve.</p>`;
            return;
        }
        grid.innerHTML = heatmap.cells.map(c => {
            const positive = c.r_value >= 0;
            const intensity = Math.min(Math.abs(c.r_value) / 2, 1);
            const bg = positive ? `rgba(34,197,94,${0.15 + intensity * 0.5})` : `rgba(239,68,68,${0.15 + intensity * 0.5})`;
            return `<div class="rounded-lg p-3 text-center border border-line" style="background:${bg}">
                <div class="text-[10px] text-slate-300 font-semibold">${c.period}</div>
                <div class="text-sm font-bold ${positive ? 'text-win' : 'text-loss'}">${c.r_value > 0 ? '+' : ''}${c.r_value}R</div>
                <div class="text-[9px] text-muted">${c.trades_count} trades</div>
            </div>`;
        }).join('');
    }

    function renderMonthlyChart(heatmap) {
        const ctx = document.getElementById('monthlyChart').getContext('2d');
        const cells = (heatmap && heatmap.cells) || [];
        if (monthlyChartInstance) monthlyChartInstance.destroy();
        monthlyChartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: cells.map(c => c.period),
                datasets: [{ data: cells.map(c => c.r_value), backgroundColor: cells.map(c => c.r_value >= 0 ? '#22C55E' : '#EF4444'), borderRadius: 6 }]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    y: { grid: { color: 'rgba(30,39,64,0.6)' }, ticks: { color: '#8B93A8' } },
                    x: { grid: { display: false }, ticks: { color: '#8B93A8', font: { size: 10 } } }
                }
            }
        });
    }

    function renderRankingsTable(rankings) {
        const body = document.getElementById('rankingsTableBody');
        if (!rankings.length) {
            body.innerHTML = `<tr><td colspan="8" class="text-center text-muted py-8">No ranked instruments yet.</td></tr>`;
            return;
        }
        body.innerHTML = rankings.map(r => `
            <tr class="border-t border-line hover:bg-void/40 transition">
                <td class="px-5 py-3 text-gold font-bold">#${r.rank}</td>
                <td class="px-5 py-3 font-semibold text-white">${r.symbol}</td>
                <td class="px-5 py-3 text-muted capitalize">${r.asset_class}</td>
                <td class="px-5 py-3 text-right">${r.total_trades}</td>
                <td class="px-5 py-3 text-right ${r.win_rate >= 50 ? 'text-win' : 'text-loss'} font-semibold">${r.win_rate.toFixed(1)}%</td>
                <td class="px-5 py-3 text-right">${r.profit_factor.toFixed(2)}</td>
                <td class="px-5 py-3 text-right">${r.avg_r.toFixed(2)}R</td>
                <td class="px-5 py-3 text-right text-xs"><span class="text-win">+${r.best_trade_pct.toFixed(1)}%</span> / <span class="text-loss">${r.worst_trade_pct.toFixed(1)}%</span></td>
            </tr>`).join('');
    }

    // ══════════════════════════════════════════════════════
    // Signals
    // ══════════════════════════════════════════════════════
    let currentSignalStatus = '';
    document.querySelectorAll('.filter-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active', 'bg-gold/15', 'text-gold', 'border-gold/30'));
            chip.classList.add('active', 'bg-gold/15', 'text-gold', 'border-gold/30');
            currentSignalStatus = chip.dataset.status;
            fetchSignals();
        });
    });

    // Adaptive price formatter
    function fmtPrice(p) {
        if (p === null || p === undefined) return '—';
        const n = Number(p);
        if (!isFinite(n) || n === 0) return '$0.00';
        const abs = Math.abs(n);
        if (abs >= 1000) return '$' + n.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
        if (abs >= 1)      return '$' + n.toLocaleString(undefined, {minimumFractionDigits: 4, maximumFractionDigits: 4});
        if (abs >= 0.01)   return '$' + n.toFixed(6);
        return '$' + n.toFixed(8);
    }

    function statusBadge(status) {
        const map = {
            // Signal statuses
            pending: 'bg-warn/15 text-warn', active: 'bg-gold/15 text-gold',
            executed: 'bg-win/15 text-win', expired: 'bg-slate-600/20 text-slate-400',
            cancelled: 'bg-loss/15 text-loss',
            // Trade statuses
            completed: 'bg-win/15 text-win',
            // User statuses
            banned: 'bg-loss/15 text-loss', trial: 'bg-gold/15 text-gold',
            inactive: 'bg-slate-600/20 text-slate-400', pending_approval: 'bg-warn/15 text-warn',
        };
        const cls = map[status] || 'bg-slate-600/20 text-slate-400';
        return `<span class="badge ${cls}">${(status || '').replace(/_/g, ' ').toUpperCase()}</span>`;
    }

    async function fetchSignals() {
        try {
            const url = currentSignalStatus ? `/api/signals/?status=${currentSignalStatus}` : '/api/signals/';
            const res = await authFetch(url);
            const json = await res.json();
            const items = (json.data && json.data.items) || json.data || [];
            document.getElementById('navSignalsBadge').textContent = items.filter(s => s.status === 'active').length || '';

            document.getElementById('signalsTableBody').innerHTML = items.length ? items.map(s => `
                <tr class="border-t border-line hover:bg-void/40 transition">
                    <td class="px-5 py-3 font-semibold text-white">${s.symbol}</td>
                    <td class="px-5 py-3"><span class="badge ${s.direction === 'long' ? 'bg-win/15 text-win' : 'bg-loss/15 text-loss'}">${(s.direction || '').toUpperCase()}</span></td>
                    <td class="px-5 py-3 text-muted text-xs">${s.strategy_type || '—'}</td>
                    <td class="px-5 py-3 text-right">${fmtPrice(s.entry_price)}</td>
                    <td class="px-5 py-3 text-right text-loss">${fmtPrice(s.stop_loss)}</td>
                    <td class="px-5 py-3 text-right text-win">${(s.take_profit_levels && s.take_profit_levels[0]) ? s.take_profit_levels[0].price.toLocaleString() : '—'}</td>
                    <td class="px-5 py-3 text-right">${(s.confidence_score ?? 0).toFixed(1)}%</td>
                    <td class="px-5 py-3 text-center">${statusBadge(s.status)}</td>
                </tr>`).join('') : `<tr><td colspan="8" class="text-center text-muted py-8">No signals match this filter yet.</td></tr>`;

            document.getElementById('miniSignalFeed').innerHTML = items.slice(0, 8).map(s => `
                <div class="flex items-center justify-between text-xs bg-void/40 rounded-lg px-3 py-2">
                    <span class="font-semibold text-white">${s.symbol}</span>
                    <span class="${s.direction === 'long' ? 'text-win' : 'text-loss'} font-bold">${(s.direction || '').toUpperCase()}</span>
                    <span class="text-muted">${(s.confidence_score ?? 0).toFixed(0)}%</span>
                </div>`).join('') || `<p class="text-xs text-muted text-center py-6">No signals yet — waiting on the next strategy run.</p>`;
        } catch (e) { console.error(e); }
    }

    // ══════════════════════════════════════════════════════
    // Trades
    // ══════════════════════════════════════════════════════
    async function fetchTrades() {
        try {
            const res = await authFetch('/api/trades/');
            const json = await res.json();
            const items = (json.data && (json.data.trades || json.data.items)) || [];
            document.getElementById('tradesTableBody').innerHTML = items.length ? items.map(t => `
                <tr class="border-t border-line hover:bg-void/40 transition">
                    <td class="px-5 py-3 font-semibold text-white">${t.symbol}</td>
                    <td class="px-5 py-3 text-muted capitalize text-xs">${t.account_type}</td>
                    <td class="px-5 py-3"><span class="badge ${t.direction === 'long' ? 'bg-win/15 text-win' : 'bg-loss/15 text-loss'}">${(t.direction || '').toUpperCase()}</span></td>
                    <td class="px-5 py-3 text-right">${fmtPrice(t.entry_price)}</td>
                    <td class="px-5 py-3 text-right">${t.exit_price ? t.exit_price.toLocaleString() : '—'}</td>
                    <td class="px-5 py-3 text-right font-semibold ${(t.pnl_percent ?? 0) >= 0 ? 'text-win' : 'text-loss'}">${t.pnl_percent != null ? (t.pnl_percent >= 0 ? '+' : '') + t.pnl_percent.toFixed(2) + '%' : '—'}</td>
                    <td class="px-5 py-3 text-right">${t.r_multiple != null ? t.r_multiple.toFixed(2) + 'R' : '—'}</td>
                    <td class="px-5 py-3 text-center">${statusBadge(t.status)}</td>
                </tr>`).join('') : `<tr><td colspan="8" class="text-center text-muted py-8">No trades recorded yet.</td></tr>`;
        } catch (e) { console.error(e); }
    }

    // ══════════════════════════════════════════════════════
    // Users
    // ══════════════════════════════════════════════════════
    let userSearchTimer = null;
    document.getElementById('userSearch').addEventListener('input', () => {
        clearTimeout(userSearchTimer);
        userSearchTimer = setTimeout(fetchUsers, 350);
    });
    document.getElementById('userStatusFilter').addEventListener('change', fetchUsers);

    async function fetchUsers() {
        try {
            const search = document.getElementById('userSearch').value;
            const status = document.getElementById('userStatusFilter').value;
            const res = await authFetch(`/api/users/?search=${encodeURIComponent(search)}&status=${status}`);
            const json = await res.json();
            const items = (json.data && json.data.items) || json.data || [];
            document.getElementById('usersTableBody').innerHTML = items.length ? items.map(u => `
                <tr class="border-t border-line hover:bg-void/40 transition">
                    <td class="px-5 py-3">
                        <div class="font-semibold text-white">${u.full_name || 'Unnamed User'}</div>
                        <div class="text-xs text-muted">@${u.telegram_username || '—'}</div>
                    </td>
                    <td class="px-5 py-3 text-muted">${u.telegram_id}</td>
                    <td class="px-5 py-3"><span class="badge ${u.role === 'admin' ? 'bg-gold/15 text-gold' : 'bg-slate-600/20 text-slate-400'}">${(u.role || '').toUpperCase()}</span></td>
                    <td class="px-5 py-3">${statusBadge(u.status)}</td>
                    <td class="px-5 py-3 text-xs text-muted">${u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}</td>
                    <td class="px-5 py-3">
                        <div class="flex items-center justify-center gap-1.5">
                            <button onclick="updateUserStatus('${u.id}','active')" title="Activate" class="w-7 h-7 rounded-lg bg-win/10 hover:bg-win/20 text-win transition"><i class="fa-solid fa-check text-xs"></i></button>
                            <button onclick="updateUserStatus('${u.id}','banned')" title="Ban" class="w-7 h-7 rounded-lg bg-loss/10 hover:bg-loss/20 text-loss transition"><i class="fa-solid fa-ban text-xs"></i></button>
                            <button onclick="updateUserStatus('${u.id}','inactive')" title="Deactivate" class="w-7 h-7 rounded-lg bg-slate-600/10 hover:bg-slate-600/20 text-slate-400 transition"><i class="fa-solid fa-pause text-xs"></i></button>
                            <button onclick="grantSubscription('${u.id}')" title="Grant Subscription" class="w-7 h-7 rounded-lg bg-gold/10 hover:bg-gold/20 text-gold transition"><i class="fa-solid fa-gift text-xs"></i></button>
                            <button onclick="viewUserDetail('${u.id}')" title="View Details" class="w-7 h-7 rounded-lg bg-void/60 hover:bg-void text-slate-300 transition"><i class="fa-solid fa-eye text-xs"></i></button>
                        </div>
                    </td>
                </tr>`).join('') : `<tr><td colspan="6" class="text-center text-muted py-8">No users match this search.</td></tr>`;
        } catch (e) { console.error(e); }
    }

    async function updateUserStatus(userId, status) {
        try {
            const res = await authFetch(`/api/users/${userId}/status?status=${status}`, { method: 'PUT' });
            if (res.ok) { showToast(`User status updated to ${status}`); fetchUsers(); }
            else { const j = await res.json().catch(() => ({})); showToast(j.detail || 'Update failed', 'error'); }
        } catch (e) { showToast('Network error', 'error'); }
    }

    async function grantSubscription(userId) {
        const days = prompt('Grant subscription for how many days? (enter a number, or "lifetime")', '30');
        if (!days) return;
        const isLifetime = days.trim().toLowerCase() === 'lifetime';
        const params = isLifetime ? 'plan_type=lifetime&days=1' : `plan_type=monthly&days=${encodeURIComponent(days)}`;
        try {
            const res = await authFetch(`/api/users/${userId}/grant-subscription?${params}`, { method: 'POST' });
            const json = await res.json();
            if (res.ok) { showToast(json.message || 'Subscription granted'); fetchUsers(); }
            else showToast(json.detail || 'Grant failed', 'error');
        } catch (e) { showToast('Network error', 'error'); }
    }

    function closeUserDetail() {
        document.getElementById('userDetailModal').classList.add('hidden');
    }

    async function viewUserDetail(userId) {
        const modal = document.getElementById('userDetailModal');
        const body = document.getElementById('userDetailBody');
        modal.classList.remove('hidden');
        body.innerHTML = `<p class="text-sm text-muted text-center py-8">Loading...</p>`;

        try {
            const res = await authFetch(`/api/users/${userId}/detail`);
            const json = await res.json();
            if (!res.ok) { body.innerHTML = `<p class="text-sm text-loss">${json.detail || 'Could not load user.'}</p>`; return; }

            const d = json.data;
            const u = d.user;
            const s = d.stats;

            body.innerHTML = `
                <div class="flex items-center justify-between mb-5">
                    <div>
                        <div class="font-bold text-white text-lg">${u.full_name || 'Unnamed'} <span class="text-muted font-normal">@${u.telegram_username || '—'}</span></div>
                        <div class="text-xs text-muted mt-1">Telegram ID: ${u.telegram_id} · Joined ${new Date(u.created_at).toLocaleDateString()}</div>
                    </div>
                    ${statusBadge(u.status)}
                </div>

                <div class="grid grid-cols-3 gap-3 mb-5">
                    <div class="bg-void/50 rounded-xl p-3 text-center"><div class="text-xl font-bold text-gold">${s.total_trades}</div><div class="text-[10px] text-muted uppercase mt-1">Trades</div></div>
                    <div class="bg-void/50 rounded-xl p-3 text-center"><div class="text-xl font-bold ${s.win_rate >= 50 ? 'text-win' : 'text-loss'}">${s.win_rate}%</div><div class="text-[10px] text-muted uppercase mt-1">Win Rate</div></div>
                    <div class="bg-void/50 rounded-xl p-3 text-center"><div class="text-xl font-bold ${s.total_pnl_usd >= 0 ? 'text-win' : 'text-loss'}">$${s.total_pnl_usd}</div><div class="text-[10px] text-muted uppercase mt-1">Total P&L</div></div>
                </div>

                <h4 class="text-xs uppercase tracking-wider text-muted font-bold mb-2">Subscriptions</h4>
                <div class="space-y-1 mb-5">
                    ${d.subscriptions.length ? d.subscriptions.map(sub => `
                        <div class="flex justify-between text-xs bg-void/40 rounded-lg px-3 py-2">
                            <span class="capitalize">${sub.plan_type} — $${sub.amount_usd}</span>
                            ${statusBadge(sub.payment_status)}
                        </div>`).join('') : '<p class="text-xs text-muted">No subscriptions.</p>'}
                </div>

                <h4 class="text-xs uppercase tracking-wider text-muted font-bold mb-2">Exchange Connections</h4>
                <div class="space-y-1 mb-5">
                    ${d.exchange_connections.length ? d.exchange_connections.map(e => `
                        <div class="flex justify-between text-xs bg-void/40 rounded-lg px-3 py-2">
                            <span class="capitalize">${e.exchange_name}</span>
                            <span class="badge ${e.connection_status === 'ok' ? 'bg-win/15 text-win' : 'bg-loss/15 text-loss'}">${e.connection_status.toUpperCase()}</span>
                        </div>`).join('') : '<p class="text-xs text-muted">No exchanges connected.</p>'}
                </div>

                <h4 class="text-xs uppercase tracking-wider text-muted font-bold mb-2">Recent Trades</h4>
                <div class="space-y-1">
                    ${d.recent_trades.length ? d.recent_trades.slice(0, 10).map(t => `
                        <div class="flex justify-between text-xs bg-void/40 rounded-lg px-3 py-2">
                            <span>${t.symbol} <span class="${t.direction === 'long' ? 'text-win' : 'text-loss'}">${t.direction.toUpperCase()}</span></span>
                            <span>${t.pnl_percent != null ? (t.pnl_percent >= 0 ? '+' : '') + t.pnl_percent.toFixed(2) + '%' : '—'}</span>
                        </div>`).join('') : '<p class="text-xs text-muted">No trades yet.</p>'}
                </div>
            `;
        } catch (e) {
            body.innerHTML = `<p class="text-sm text-loss">Network error loading user detail.</p>`;
        }
    }

    async function sendBroadcast() {
        const text = document.getElementById('broadcastText').value.trim();
        if (!text) { showToast('Write a message first', 'error'); return; }
        if (!confirm(`Send this to ALL active/trial users?\n\n"${text}"`)) return;
        try {
            const res = await authFetch(`/api/users/broadcast?message=${encodeURIComponent(text)}`, { method: 'POST' });
            const json = await res.json();
            if (res.ok) {
                showToast(`Sent to ${json.data.sent}/${json.data.total_recipients} users`);
                document.getElementById('broadcastText').value = '';
            } else showToast(json.detail || 'Broadcast failed', 'error');
        } catch (e) { showToast('Network error', 'error'); }
    }

    // ══════════════════════════════════════════════════════
    // Payments
    // ══════════════════════════════════════════════════════
    async function fetchPayments() {
        try {
            const res = await authFetch('/api/payments/pending');
            const json = await res.json();
            const items = json.data || [];
            document.getElementById('navPaymentsBadge').textContent = items.length || '';

            document.getElementById('paymentsTableBody').innerHTML = items.length ? items.map(p => `
                <tr class="border-t border-line hover:bg-void/40 transition">
                    <td class="px-5 py-3">
                        <div class="font-semibold text-white">@${p.telegram_username || 'unknown'}</div>
                        <div class="text-xs text-muted">${p.telegram_id || ''}</div>
                    </td>
                    <td class="px-5 py-3 capitalize">${p.plan_type}</td>
                    <td class="px-5 py-3 text-right">$${p.amount_usd} ${p.crypto_currency || ''}</td>
                    <td class="px-5 py-3 text-xs font-mono text-muted">${p.crypto_tx_hash ? p.crypto_tx_hash.slice(0, 14) + '…' : '—'}</td>
                    <td class="px-5 py-3 text-xs text-muted">${p.submitted_at ? new Date(p.submitted_at).toLocaleString() : '—'}</td>
                    <td class="px-5 py-3">
                        <div class="flex items-center justify-center gap-1.5">
                            <button onclick="confirmPayment('${p.subscription_id}', true)" title="Approve" class="w-8 h-8 rounded-lg bg-win/10 hover:bg-win/20 text-win transition"><i class="fa-solid fa-check text-xs"></i></button>
                            <button onclick="confirmPayment('${p.subscription_id}', false)" title="Reject" class="w-8 h-8 rounded-lg bg-loss/10 hover:bg-loss/20 text-loss transition"><i class="fa-solid fa-xmark text-xs"></i></button>
                        </div>
                    </td>
                </tr>`).join('') : `<tr><td colspan="6" class="text-center text-muted py-8">No pending payments to review.</td></tr>`;
        } catch (e) { console.error(e); }
    }

    async function confirmPayment(subscriptionId, approved) {
        try {
            const res = await authFetch(`/api/payments/crypto/confirm?subscription_id=${subscriptionId}&approved=${approved}`, { method: 'POST' });
            const json = await res.json();
            if (res.ok) { showToast(json.message || 'Done'); fetchPayments(); }
            else showToast(json.detail || 'Action failed', 'error');
        } catch (e) { showToast('Network error', 'error'); }
    }

    // ══════════════════════════════════════════════════════
    // Audit Log
    // ══════════════════════════════════════════════════════
    function actionBadge(action) {
        const map = {
            user_status_changed: 'bg-gold/15 text-gold', trial_approved: 'bg-win/15 text-win',
            trial_rejected: 'bg-loss/15 text-loss', payment_confirmed: 'bg-win/15 text-win',
            payment_rejected: 'bg-loss/15 text-loss', signal_created_manually: 'bg-gold/15 text-gold',
            strategy_triggered_manually: 'bg-gold/15 text-gold',
        };
        return `<span class="badge ${map[action] || 'bg-slate-600/20 text-slate-400'}">${(action || '').replace(/_/g, ' ')}</span>`;
    }

    async function fetchAuditLog() {
        try {
            const res = await authFetch('/api/users/audit-log');
            const json = await res.json();
            const items = (json.data && json.data.items) || [];
            document.getElementById('auditLogTableBody').innerHTML = items.length ? items.map(e => `
                <tr class="border-t border-line hover:bg-void/40 transition">
                    <td class="px-5 py-3 text-xs text-muted whitespace-nowrap">${new Date(e.created_at).toLocaleString()}</td>
                    <td class="px-5 py-3 text-xs">${e.admin_telegram_id ?? '—'}</td>
                    <td class="px-5 py-3">${actionBadge(e.action)}</td>
                    <td class="px-5 py-3 text-xs text-muted">${e.target_type || ''} ${e.target_id ? '#' + e.target_id.slice(0, 8) : ''}</td>
                    <td class="px-5 py-3 text-xs text-muted font-mono">${e.details ? JSON.stringify(e.details) : '—'}</td>
                </tr>`).join('') : `<tr><td colspan="5" class="text-center text-muted py-8">No admin actions recorded yet.</td></tr>`;
        } catch (e) { console.error(e); }
    }

    // ══════════════════════════════════════════════════════
    // Strategy trigger
    // ══════════════════════════════════════════════════════
    async function triggerStrategies() {
        try {
            const response = await authFetch('/api/strategies/run', { method: 'POST' });
            if (response.ok) { showToast('Strategy engines triggered — signals will populate shortly.'); }
            else { const j = await response.json().catch(() => ({})); showToast(j.detail || 'Trigger failed', 'error'); }
        } catch (e) { showToast('Network error', 'error'); }
    }

    // ══════════════════════════════════════════════════════
    // Boot
    // ══════════════════════════════════════════════════════
    document.addEventListener('DOMContentLoaded', () => {
        ensureAdminToken();
        fetchOverview();
        fetchSignals();
        fetchTrades();
        fetchUsers();
        fetchPayments();
    });
</script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


# ── Mount All API Routers ───────────────────────────────
from backend.api.auth import router as auth_router, require_admin
from backend.api.users import router as users_router
from backend.api.signals import router as signals_router
from backend.api.trades import router as trades_router
from backend.api.analytics import router as analytics_router
from backend.api.payments import router as payments_router, webhook_router
from backend.api.account import router as account_router

app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(users_router, prefix="/api/users", tags=["Users"])
app.include_router(signals_router, prefix="/api/signals", tags=["Signals"])
app.include_router(trades_router, prefix="/api/trades", tags=["Trades"])
app.include_router(analytics_router, prefix="/api/analytics", tags=["Analytics"])
app.include_router(payments_router, prefix="/api/payments", tags=["Payments"])
app.include_router(account_router, prefix="/api/account", tags=["Account"])
app.include_router(webhook_router, prefix="/api/webhooks", tags=["Webhooks"])


# ── Manual Strategy Trigger ─────────────────────────────
@app.post("/api/strategies/run", tags=["Strategies"])
async def trigger_strategies(_admin=Depends(require_admin)):
    """Manually trigger all strategy engines to run. Admin-only.

    Note: this is now a *supplement* to the automatic scheduler in
    `lifespan()`, which already runs this every
    settings.STRATEGY_RUN_INTERVAL_MINUTES minutes. Use this button for
    an on-demand run, not as the only way signals get generated.
    """
    from backend.strategies.runner import run_all_strategies
    import asyncio
    asyncio.create_task(run_all_strategies())
    return {"status": "started", "message": "Strategies are running in background."}


# ── Run server ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=settings.DEBUG,
    )
