"""
SmAttaker — Central Configuration
Loads from environment variables with sensible defaults.
"""
from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Optional


class Settings(BaseSettings):
    # ── App ──────────────────────────────────────────────
    APP_NAME: str = "SmAttaker"
    APP_ENV: str = "production"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"
    ENCRYPTION_KEY: str = ""  # Fernet key for encrypting exchange API keys

    # ── Database ──────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/smattaker"
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 10

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def ensure_asyncpg_prefix(cls, v: str) -> str:
        if v:
            # If it starts with standard postgresql:// or postgres://, convert to asyncpg
            if v.startswith("postgres://"):
                return v.replace("postgres://", "postgresql+asyncpg://", 1)
            elif v.startswith("postgresql://") and not v.startswith("postgresql+asyncpg://"):
                return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # ── Redis ─────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Telegram ──────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_USERNAME: str = "SmAttakerBot"
    TELEGRAM_ADMIN_CHAT_ID: str = ""

    # ── Admin ─────────────────────────────────────────────
    ADMIN_EMAIL: str = "amanossama@gmail.com"
    ADMIN_TELEGRAM_ID: str = ""

    # ── MetaApi (MT5 auto-provisioning) ────────────────────
    # v54: the operator's OWN MetaApi organization token, set ONCE as
    # an env var. With this set, users never touch MetaApi's dashboard
    # or paste an Account ID/Token themselves — they type their normal
    # MT5 login/password/broker-server (the 3 fields the dashboard
    # already collects) and the backend provisions the MetaApi account
    # FOR them via the SDK. See backend/exchange/mt5_connector.py's
    # `provision_account()`. Free tier covers 1 account; paid tiers
    # scale to many — sufficient for a single-operator platform serving
    # many users under one MetaApi organization.
    METAAPI_TOKEN: str = ""

    # ── NOWPayments (Crypto Payments) ───────────────────
    NOWPAYMENTS_API_KEY: str = ""
    NOWPAYMENTS_IPN_SECRET: str = ""
    NOWPAYMENTS_API_URL: str = "https://api.nowpayments.io/v1"

    # ── Manual Wallet Addresses (Direct Payments) ──────────
    # ⚠️ CRITICAL FIX: previously a single `USDT_WALLET_ADDRESS` was
    # shown to customers labeled "TRC20/ERC20" — but a TRON (TRC20)
    # address and an Ethereum (ERC20) address are fundamentally
    # different formats (different base encoding, different prefix,
    # different chain). ONE address can never be valid for both
    # networks. A customer sending USDT-TRC20 to what was actually an
    # ERC20 address (or vice versa) would very likely lose the funds
    # permanently. Each network now has its own explicit address.
    USDT_TRC20_ADDRESS: str = ""   # TRON network — starts with "T"
    USDT_ERC20_ADDRESS: str = ""   # Ethereum network — starts with "0x"
    USDT_BEP20_ADDRESS: str = ""   # BNB Smart Chain — starts with "0x"
    BTC_WALLET_ADDRESS: str = ""   # Bitcoin network — starts with "1", "3", or "bc1"
    # ⚠️ V52: BTC on BEP20 (BNB Smart Chain) — a wrapped BTC token on the
    # BSC network. Starts with "0x" like other EVM addresses. This is
    # separate from BTC_WALLET_ADDRESS (native Bitcoin) because the two
    # networks are incompatible — sending native BTC to a BEP20 address
    # or vice versa would lose the funds permanently.
    BTC_BEP20_ADDRESS: str = ""    # BTC on BEP20 (BNB Smart Chain) — starts with "0x"

    # ── Subscription ──────────────────────────────────────
    SUBSCRIPTION_PRICE_USD: float = 49.0
    TRIAL_DAYS: int = 3
    DEFAULT_LANGUAGE: str = "en"

    # ── Trading ───────────────────────────────────────────
    MAX_DAILY_SIGNALS: int = 50
    # v45.4.6 FIX: was 60 (1 hour) — every signal was expiring an hour
    # after creation, contradicting the broadcast message's promise of
    # "I'll notify you when SL or TP is hit, or after 8 hours." This was
    # the root cause of the user's screenshot showing "SIGNAL EXPIRED —
    # price feed unavailable" just one hour after a signal fired.
    # Now: 480 minutes = 8 hours (matches the broadcast message).
    SIGNAL_EXPIRY_MINUTES: int = 480
    DEFAULT_LEVERAGE: int = 10
    MAX_LEVERAGE: int = 125

    # ── Strategy Engine ───────────────────────────────────
    # V45.4.1 (APEX) - unified engine (crypto + gold + commodity + forex +
    # stocks + index futures)
    # All asset classes are handled by one leak-free meta-labeling pipeline.
    # The engine itself hardcodes INTERVAL='1h', SL_ATR=2.0, TP_RR=2.0,
    # MAX_BARS=8 - these are training constants and must NOT be overridden
    # at runtime (doing so would desync live inference from the trained models).
    STRATEGY_TIMEFRAME: str = "1h"          # v45.4.1 trained on 1h bars
    STRATEGY_FETCH_LIMIT: int = 1000        # bars to fetch per asset
    STRATEGY_MIN_BARS: int = 250            # minimum bars for EMA200 warmup
    STRATEGY_LIVE_BAR_LOOKBACK: int = 2     # check last N closed bars
    STRATEGY_ENABLED: bool = True           # master switch for the unified engine
    # Data fetcher cache TTL (seconds)
    DATA_CACHE_TTL: int = 300

    # ── CORS ─────────────────────────────────────────────
    # Comma-separated list of allowed origins, e.g. "https://app.example.com".
    # Leave empty during early development; set it before going live.
    CORS_ALLOWED_ORIGINS: str = ""

    # ── Market Data Providers ───────────────────────────────
    # Twelve Data has an official, documented REST API (unlike yfinance,
    # which scrapes Yahoo's undocumented internal endpoints and gets
    # blocked from cloud/datacenter IPs like Render's). Free tier covers
    # forex + stocks + gold/commodities from one consistent provider.
    # If empty, the v45.4.1 unified strategy falls back to yfinance automatically.
    TWELVE_DATA_API_KEY: str = ""

    # ── Deployment ────────────────────────────────────────
    PORT: int = 8000
    RENDER_EXTERNAL_URL: str = "http://localhost:8000"
    WEBHOOK_URL: str = "http://localhost:8000/api/webhooks"

    # ── JWT ───────────────────────────────────────────────
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── Internal Service-to-Service Auth ───────────────────
    # Required header (X-Internal-Api-Key) for endpoints that should only
    # ever be called by our own bot/scheduler, never by an end user.
    INTERNAL_API_KEY: str = ""

    # ── Strategy Scheduler ──────────────────────────────────
    # How often the strategy engines run automatically in the background.
    # Previously this was documented as "Celery Beat" but no such worker
    # was ever wired up anywhere in the project, so signals were NEVER
    # generated automatically. Now handled via APScheduler in main.py.
    STRATEGY_RUN_INTERVAL_MINUTES: int = 15

    # ── Black Swan (strategy #2 — SNIPER BODY NOLDN v22 port) ───────────
    # Master switch for the Black Swan scheduler job. The engine's trading
    # constants are FROZEN (validated over 2018→2026) and are NOT runtime-
    # configurable by design — doing so would desync live behavior from the
    # validated book, exactly like overriding V45's training constants.
    BLACK_SWAN_ENABLED: bool = True
    # Black Swan card lifecycle: 7440 min = 124h = the book's longest full
    # trade lifecycle (240×30m time stop = 120h) + the 2×30m resting-limit
    # order window + a small tail. The monitor reads signal.expiry_minutes,
    # so this — not SIGNAL_EXPIRY_MINUTES — governs Black Swan cards.
    BLACK_SWAN_SIGNAL_EXPIRY_MINUTES: int = 7440
    # 30m bars fetched per asset per analysis (~125 days) — enough for the
    # frozen pipeline's daily EMA50-slope gate without wasting API budget.
    BLACK_SWAN_FETCH_BARS: int = 6000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


# Singleton
settings = Settings()
