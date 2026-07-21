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

    # ── Subscription ──────────────────────────────────────
    SUBSCRIPTION_PRICE_USD: float = 99.0
    TRIAL_DAYS: int = 3
    DEFAULT_LANGUAGE: str = "en"

    # ── Trading ───────────────────────────────────────────
    MAX_DAILY_SIGNALS: int = 50
    SIGNAL_EXPIRY_MINUTES: int = 60
    DEFAULT_LEVERAGE: int = 10
    MAX_LEVERAGE: int = 125

    # ── Strategy Engine ───────────────────────────────────
    # Singularity v40 (crypto)
    CRYPTO_TIMEFRAME: str = "30m"
    CRYPTO_FETCH_LIMIT: int = 1000
    CRYPTO_MIN_BARS: int = 500
    CRYPTO_LIVE_BAR_LOOKBACK: int = 3
    # Aurum v2 (gold/forex/stocks)
    AURUM_FETCH_LIMIT: int = 500
    AURUM_MIN_BARS: int = 250
    AURUM_BARS_FOR_SIGNAL: int = 300
    # Data fetcher cache TTL (seconds)
    DATA_CACHE_TTL: int = 300
    # Enable/disable each engine independently
    CRYPTO_STRATEGY_ENABLED: bool = True
    GOLD_FOREX_STRATEGY_ENABLED: bool = True

    # ── CORS ─────────────────────────────────────────────
    # Comma-separated list of allowed origins, e.g. "https://app.example.com".
    # Leave empty during early development; set it before going live.
    CORS_ALLOWED_ORIGINS: str = ""

    # ── Market Data Providers ───────────────────────────────
    # Twelve Data has an official, documented REST API (unlike yfinance,
    # which scrapes Yahoo's undocumented internal endpoints and gets
    # blocked from cloud/datacenter IPs like Render's). Free tier covers
    # forex + stocks + gold/commodities from one consistent provider.
    # If empty, gold_forex_strategy falls back to yfinance automatically.
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

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


# Singleton
settings = Settings()
