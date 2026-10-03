"""
SmAttaker — Account API Routes
Exchange connections + risk settings management for the authenticated user.

⚠️ WHY THIS FILE EXISTS:
Before this, `ExchangeConnection` and `RiskSettings` were full, well-designed
DB models with NO API surface at all — nothing let a user actually connect
an exchange or configure their risk settings through the web dashboard or
any HTTP endpoint. Real trading (`trade_executor.py`) depends on both of
these existing, so without this file the "Real" account type could never
actually be used by anyone — a structural gap, not a cosmetic one.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field, field_validator
from typing import Any, Optional
from datetime import datetime, timezone
import uuid

from backend.database import get_db
from backend.config import settings
from backend.models.user import User
from backend.models.exchange_connection import ExchangeConnection
from backend.models.risk_settings import RiskSettings
from backend.models.subscription import Subscription
from backend.schemas.common import APIResponse
from backend.schemas.user import UserOut
from backend.api.auth import get_current_user_dep
from backend.utils.security import encrypt_api_key
from backend.utils.rate_limit import rate_limiter
from backend.exchange.connector import ExchangeConnector

router = APIRouter()


def _decrypt_mt5_server(conn) -> Optional[str]:
    """Decrypt the MT5 server name from passphrase_encrypted.

    The MT5 server name (e.g. 'ICMarketsSC-Demo') is stored in the
    passphrase_encrypted column. Unlike a real passphrase, the server
    name is NOT a secret — it's the broker's public hostname. We
    decrypt it here so the UI can display it.
    """
    if not conn.passphrase_encrypted:
        return None
    try:
        from backend.utils.security import decrypt_api_key
        return decrypt_api_key(conn.passphrase_encrypted)
    except Exception:
        return None


# ── Schemas (local to this router — thin, request/response only) ──
class ExchangeConnectionCreate(BaseModel):
    exchange_name: str  # 'binance', 'bybit', 'mt5', etc.
    exchange_label: Optional[str] = None
    api_key: str  # API key OR MT5 login OR MetaApi Account ID
    secret_key: str  # Secret key OR MT5 password OR MetaApi API token
    passphrase: Optional[str] = None  # OKX passphrase OR MT5 server name
    is_testnet: bool = False  # testnet for exchanges, demo for MT5
    # v45 MT5 live bridge: when exchange_name == 'mt5' AND both fields
    # below are filled, the connector uses MetaApi Cloud SDK for real
    # order execution. When empty, falls back to stub mode.
    metaapi_account_id: Optional[str] = None
    metaapi_api_token: Optional[str] = None


class ExchangeConnectionOut(BaseModel):
    id: str
    exchange_name: str
    exchange_label: Optional[str] = None
    is_active: bool
    is_testnet: bool
    connection_status: str
    connection_error: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    # ⚠️ Never return decrypted keys, and never even return the ciphertext —
    # the client has no legitimate use for it and it needlessly widens the
    # blast radius if a token ever leaks.
    api_key_preview: str = ""
    # MT5-specific (only populated for exchange_name='mt5'); empty for CCXT exchanges
    mt5_server: Optional[str] = None

    class Config:
        from_attributes = True

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_uuid_to_str(cls, v: Any) -> str:
        """Coerce uuid.UUID → str. Pydantic v2 strict mode refuses to
        auto-coerce, and SQLAlchemy returns a UUID object for UUID columns."""
        if v is None:
            return ""
        if isinstance(v, uuid.UUID):
            return str(v)
        return str(v)


class RiskSettingsUpdate(BaseModel):
    account_type: str = "demo"
    max_risk_per_trade_pct: Optional[float] = Field(None, gt=0, le=10)
    max_daily_risk_pct: Optional[float] = Field(None, gt=0, le=50)
    max_open_positions: Optional[int] = Field(None, ge=1, le=50)
    max_leverage: Optional[int] = Field(None, ge=1, le=125)
    position_sizing_method: Optional[str] = None
    fixed_position_size: Optional[float] = Field(None, gt=0)
    risk_reward_min_ratio: Optional[float] = Field(None, ge=0)


class RiskSettingsOut(BaseModel):
    id: str
    account_type: str
    name: str
    max_risk_per_trade_pct: float
    max_daily_risk_pct: float
    max_weekly_risk_pct: float
    max_open_positions: int
    max_leverage: int
    position_sizing_method: str
    fixed_position_size: float
    risk_reward_min_ratio: float
    is_active: bool

    class Config:
        from_attributes = True

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_uuid_to_str(cls, v: Any) -> str:
        """Coerce uuid.UUID → str. Pydantic v2 strict mode refuses to
        auto-coerce, and SQLAlchemy returns a UUID object for UUID columns."""
        if v is None:
            return ""
        if isinstance(v, uuid.UUID):
            return str(v)
        return str(v)


class SubscriptionOut(BaseModel):
    id: str
    plan_type: str
    payment_status: str
    amount_usd: float
    start_date: datetime
    end_date: Optional[datetime] = None
    auto_renew: bool

    class Config:
        from_attributes = True

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_uuid_to_str(cls, v: Any) -> str:
        """Coerce uuid.UUID → str. Pydantic v2 strict mode refuses to
        auto-coerce, and SQLAlchemy returns a UUID object for UUID columns."""
        if v is None:
            return ""
        if isinstance(v, uuid.UUID):
            return str(v)
        return str(v)


class AccountProfile(BaseModel):
    """Everything the user-facing dashboard needs in one call."""
    user: UserOut
    subscriptions: list[SubscriptionOut]
    risk_settings: list[RiskSettingsOut]
    exchange_connections: list[ExchangeConnectionOut]


# ── Full profile bundle for the dashboard ───────────────
@router.get("/me/full", response_model=APIResponse[AccountProfile])
async def get_full_profile(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """Single call that powers the whole user dashboard: profile,
    subscriptions, risk settings, and exchange connections."""
    subs_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.start_date.desc())
    )
    subs = subs_result.scalars().all()

    risk_result = await db.execute(select(RiskSettings).where(RiskSettings.user_id == user.id))
    risk = risk_result.scalars().all()

    exch_result = await db.execute(select(ExchangeConnection).where(ExchangeConnection.user_id == user.id))
    exch = exch_result.scalars().all()

    return APIResponse(data=AccountProfile(
        user=UserOut.model_validate(user),
        subscriptions=[SubscriptionOut.model_validate(s) for s in subs],
        risk_settings=[RiskSettingsOut.model_validate(r) for r in risk],
        exchange_connections=[
            ExchangeConnectionOut(
                id=str(e.id), exchange_name=e.exchange_name, exchange_label=e.exchange_label,
                is_active=e.is_active, is_testnet=e.is_testnet, connection_status=e.connection_status,
                connection_error=e.connection_error, last_checked_at=e.last_checked_at,
                api_key_preview=f"••••{e.api_key_encrypted[-4:]}" if e.api_key_encrypted else "",
                mt5_server=(
                    # Decrypt the MT5 server name for display — it's stored
                    # in passphrase_encrypted but is not actually a secret
                    # (it's the broker's public server hostname).
                    _decrypt_mt5_server(e) if e.exchange_name.lower() == "mt5" else None
                ),
            ) for e in exch
        ],
    ))


# ── Connect a new exchange ──────────────────────────────
@router.post(
    "/exchange",
    response_model=APIResponse[ExchangeConnectionOut],
    dependencies=[Depends(rate_limiter(max_requests=10, window_seconds=300, prefix="exchange_connect"))],
)
async def connect_exchange(
    payload: ExchangeConnectionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """
    Connect a new trading platform. Supports:
      - CCXT crypto exchanges (Binance, Bybit, OKX, MEXC, KuCoin, etc.)
      - MetaTrader 5 (MT5) — for forex/CFD trading

    Keys are encrypted at rest (never stored in plaintext) and immediately
    test-pinged so the user finds out right away if a key is bad, instead
    of discovering it only when a real trade silently fails later.
    """
    exchange_lower = payload.exchange_name.lower()

    # ── Validate exchange_name ──
    is_mt5 = exchange_lower == "mt5"
    if not is_mt5 and exchange_lower not in ExchangeConnector.EXCHANGE_CLASS_MAP:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported platform '{payload.exchange_name}'. "
                f"Supported exchanges: {ExchangeConnector.get_supported_exchanges()}. "
                "Use 'mt5' for MetaTrader 5."
            ),
        )

    conn = ExchangeConnection(
        user_id=user.id,
        exchange_name=exchange_lower,
        exchange_label=payload.exchange_label or (
            "MetaTrader 5" if is_mt5 else payload.exchange_name.title()
        ),
        api_key_encrypted=encrypt_api_key(payload.api_key),
        secret_key_encrypted=encrypt_api_key(payload.secret_key),
        passphrase_encrypted=encrypt_api_key(payload.passphrase) if payload.passphrase else None,
        is_testnet=payload.is_testnet,
        is_active=True,
        connection_status="unknown",
    )
    db.add(conn)
    await db.flush()

    # ── Live-test the credentials right away ──
    # For MT5 we use the MT5Connector; for everything else, CCXT.
    try:
        if is_mt5:
            from backend.exchange.mt5_connector import MT5Connector
            # For MT5 there are now three modes, tried in this order:
            #   (a) Explicit MetaApi mode: user pasted their own
            #       metaapi_account_id + token (advanced/manual path,
            #       still supported for anyone who prefers it).
            #   (b) ⚠️ v54 Auto-connect mode (THE self-service fix): user
            #       gave only login/password/server — if the operator has
            #       configured settings.METAAPI_TOKEN, we provision a
            #       MetaApi account FOR them automatically. No MetaApi
            #       dashboard, no manual admin work, nothing to install.
            #   (c) Legacy stub mode: METAAPI_TOKEN not configured on this
            #       platform — falls back to format-only validation, same
            #       as before v54.
            if payload.metaapi_account_id and payload.metaapi_api_token:
                # (a) Explicit MetaApi mode — override the stored encrypted
                # keys with the MetaApi credentials the user provided.
                conn.api_key_encrypted = encrypt_api_key(payload.metaapi_account_id)
                conn.secret_key_encrypted = encrypt_api_key(payload.metaapi_api_token)
                if payload.passphrase:
                    conn.passphrase_encrypted = encrypt_api_key(payload.passphrase)
                mt5_conn = MT5Connector(
                    login=payload.api_key,           # MT5 login (display only)
                    password=payload.secret_key,     # MT5 password (display only)
                    server=payload.passphrase or "",
                    is_demo=payload.is_testnet,
                    metaapi_account_id=payload.metaapi_account_id,
                    metaapi_api_token=payload.metaapi_api_token,
                )
            elif settings.METAAPI_TOKEN:
                # (b) Auto-connect — the actual self-service flow.
                provision = await MT5Connector.provision_account(
                    login=payload.api_key,
                    password=payload.secret_key,
                    server=payload.passphrase or "",
                    is_demo=payload.is_testnet,
                    name=f"{payload.api_key}-{conn.user_id}",
                )
                if not provision.get("success"):
                    raise HTTPException(status_code=400, detail=provision.get("error", "MT5 auto-connect failed."))
                metaapi_account_id = provision["metaapi_account_id"]
                conn.api_key_encrypted = encrypt_api_key(metaapi_account_id)
                conn.secret_key_encrypted = encrypt_api_key(settings.METAAPI_TOKEN)
                if payload.passphrase:
                    conn.passphrase_encrypted = encrypt_api_key(payload.passphrase)
                mt5_conn = MT5Connector(
                    login=payload.api_key,
                    password=payload.secret_key,
                    server=payload.passphrase or "",
                    is_demo=payload.is_testnet,
                    metaapi_account_id=metaapi_account_id,
                    metaapi_api_token=settings.METAAPI_TOKEN,
                )
            else:
                # (c) Legacy stub mode — just validate login/password format
                mt5_conn = MT5Connector(
                    login=payload.api_key,
                    password=payload.secret_key,
                    server=payload.passphrase or "",
                    is_demo=payload.is_testnet,
                )
            test_result = await mt5_conn.test_connection()
        else:
            connector = ExchangeConnector(
                exchange_name=conn.exchange_name,
                api_key_encrypted=conn.api_key_encrypted,
                secret_key_encrypted=conn.secret_key_encrypted,
                passphrase_encrypted=conn.passphrase_encrypted,
                is_testnet=conn.is_testnet,
            )
            test_result = await connector.test_connection()

        # ⚠️ Geo-block soft-success: if the test failed ONLY because the
        # server can't reach the exchange from its region (Bybit's
        # CloudFront is notorious for this on US-based cloud IPs), we
        # still mark the connection as "ok" — the credentials may be
        # valid, and the user can verify with a small test trade. The
        # warning text is forwarded to the UI so the user understands
        # why they're seeing a yellow banner despite the green "saved".
        if test_result.get("success"):
            conn.connection_status = "ok"
            conn.connection_error = None
            # Stash the geo-block warning on the connection row so the
            # UI can keep showing it (and so future loads don't lose it).
            if test_result.get("geo_blocked"):
                conn.connection_error = test_result.get("warning")
        else:
            conn.connection_status = "error"
            conn.connection_error = test_result.get("error")
        conn.last_checked_at = datetime.now(timezone.utc)
    except Exception as e:
        conn.connection_status = "error"
        conn.connection_error = str(e)

    await db.flush()
    await db.refresh(conn)

    # Reveal the MT5 server name in the output (it's stored encrypted in
    # passphrase_encrypted, but the server name is not actually a secret —
    # it's the broker's public server hostname like "ICMarketsSC-Demo").
    mt5_server_out = None
    if is_mt5 and payload.passphrase:
        mt5_server_out = payload.passphrase

    # Build the response message — different tone for "ok", "ok with
    # geo-block warning", and "error".
    if conn.connection_status == "ok":
        if test_result and test_result.get("geo_blocked"):
            message = test_result.get("warning")
        else:
            message = "Platform connected successfully."
    else:
        message = f"Platform saved, but the connection test failed: {conn.connection_error}"

    return APIResponse(
        data=ExchangeConnectionOut(
            id=str(conn.id), exchange_name=conn.exchange_name, exchange_label=conn.exchange_label,
            is_active=conn.is_active, is_testnet=conn.is_testnet, connection_status=conn.connection_status,
            connection_error=conn.connection_error, last_checked_at=conn.last_checked_at,
            api_key_preview=f"••••{conn.api_key_encrypted[-4:]}",
            mt5_server=mt5_server_out,
        ),
        message=message,
    )


# ── Toggle / disconnect an exchange ─────────────────────
@router.put("/exchange/{connection_id}/toggle", response_model=APIResponse[dict])
async def toggle_exchange(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """Enable/disable an exchange connection without deleting the stored keys."""
    result = await db.execute(
        select(ExchangeConnection).where(
            ExchangeConnection.id == connection_id, ExchangeConnection.user_id == user.id
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Exchange connection not found.")
    conn.is_active = not conn.is_active
    await db.flush()
    return APIResponse(data={"is_active": conn.is_active})


@router.delete("/exchange/{connection_id}", response_model=APIResponse[dict])
async def delete_exchange(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """Permanently remove an exchange connection and its encrypted keys."""
    result = await db.execute(
        select(ExchangeConnection).where(
            ExchangeConnection.id == connection_id, ExchangeConnection.user_id == user.id
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Exchange connection not found.")
    await db.delete(conn)
    await db.flush()
    return APIResponse(data={"deleted": True})


# ── Set account type (REAL / DEMO / PAPER onboarding) ───
class AccountTypeUpdate(BaseModel):
    """Web onboarding: user must choose a trade type before doing anything."""
    account_type: str = Field(..., pattern="^(real|demo|paper)$")


@router.put("/me/account-type", response_model=APIResponse[UserOut])
async def set_account_type(
    payload: AccountTypeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """Set the user's default account type from the web dashboard.

    This is the web equivalent of the Telegram bot's onboarding flow —
    a new user MUST choose REAL / DEMO / PAPER before they can use the
    system. Setting it also flips `onboarding_completed` to True so
    the dashboard's "Getting Started" checklist marks step 1 done.

    For PAPER accounts we also seed the virtual balance to $10,000
    if the user doesn't already have one (same as the bot onboarding).
    """
    user.default_account_type = payload.account_type
    user.onboarding_completed = True

    # Seed paper balance on first switch to PAPER
    if payload.account_type == "paper" and not user.paper_balance:
        user.paper_balance = 10000.0

    await db.flush()
    await db.refresh(user)
    return APIResponse(
        data=UserOut.model_validate(user),
        message=f"Account type set to {payload.account_type.upper()}.",
    )


# ── Risk settings ────────────────────────────────────────
@router.put("/risk", response_model=APIResponse[RiskSettingsOut])
async def update_risk_settings(
    payload: RiskSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """
    Create or update the user's risk settings for a given account type
    (demo/real). This is what trade_executor.py actually reads at
    execution time for position sizing and leverage — without this
    endpoint a user had no way to ever set it away from the defaults.
    """
    result = await db.execute(
        select(RiskSettings).where(
            RiskSettings.user_id == user.id,
            RiskSettings.account_type == payload.account_type,
        )
    )
    risk = result.scalar_one_or_none()
    if not risk:
        risk = RiskSettings(user_id=user.id, account_type=payload.account_type, is_default=True)
        db.add(risk)

    for field in (
        "max_risk_per_trade_pct", "max_daily_risk_pct", "max_open_positions",
        "max_leverage", "position_sizing_method", "fixed_position_size",
        "risk_reward_min_ratio",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(risk, field, value)

    await db.flush()
    await db.refresh(risk)
    return APIResponse(data=RiskSettingsOut.model_validate(risk), message="Risk settings updated.")
