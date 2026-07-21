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
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timezone

from backend.database import get_db
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


# ── Schemas (local to this router — thin, request/response only) ──
class ExchangeConnectionCreate(BaseModel):
    exchange_name: str
    exchange_label: Optional[str] = None
    api_key: str
    secret_key: str
    passphrase: Optional[str] = None
    is_testnet: bool = False


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

    class Config:
        from_attributes = True


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
    Connect a new exchange account. Keys are encrypted at rest
    (never stored in plaintext) and immediately test-pinged so the user
    finds out right away if a key is bad, instead of discovering it only
    when a real trade silently fails later.
    """
    if payload.exchange_name.lower() not in ExchangeConnector.EXCHANGE_CLASS_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported exchange. Supported: {ExchangeConnector.get_supported_exchanges()}",
        )

    conn = ExchangeConnection(
        user_id=user.id,
        exchange_name=payload.exchange_name.lower(),
        exchange_label=payload.exchange_label or payload.exchange_name.title(),
        api_key_encrypted=encrypt_api_key(payload.api_key),
        secret_key_encrypted=encrypt_api_key(payload.secret_key),
        passphrase_encrypted=encrypt_api_key(payload.passphrase) if payload.passphrase else None,
        is_testnet=payload.is_testnet,
        is_active=True,
        connection_status="unknown",
    )
    db.add(conn)
    await db.flush()

    # Live-test the credentials right away.
    try:
        connector = ExchangeConnector(
            exchange_name=conn.exchange_name,
            api_key_encrypted=conn.api_key_encrypted,
            secret_key_encrypted=conn.secret_key_encrypted,
            passphrase_encrypted=conn.passphrase_encrypted,
            is_testnet=conn.is_testnet,
        )
        test_result = await connector.test_connection()
        conn.connection_status = "ok" if test_result.get("success") else "error"
        conn.connection_error = None if test_result.get("success") else test_result.get("error")
        conn.last_checked_at = datetime.now(timezone.utc)
    except Exception as e:
        conn.connection_status = "error"
        conn.connection_error = str(e)

    await db.flush()
    await db.refresh(conn)

    return APIResponse(
        data=ExchangeConnectionOut(
            id=str(conn.id), exchange_name=conn.exchange_name, exchange_label=conn.exchange_label,
            is_active=conn.is_active, is_testnet=conn.is_testnet, connection_status=conn.connection_status,
            connection_error=conn.connection_error, last_checked_at=conn.last_checked_at,
            api_key_preview=f"••••{conn.api_key_encrypted[-4:]}",
        ),
        message=(
            "Exchange connected successfully." if conn.connection_status == "ok"
            else f"Exchange saved, but the connection test failed: {conn.connection_error}"
        ),
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
