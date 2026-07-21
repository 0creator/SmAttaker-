"""
SmAttaker — Signals API Routes
Signal creation, broadcast, listing.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from backend.database import get_db
from backend.models.signal import Signal, SignalStatus
from backend.models.user import User
from backend.schemas.signal import SignalOut, SignalCreate
from backend.schemas.common import APIResponse, PaginatedResponse
from backend.api.auth import require_admin, get_current_user_dep

router = APIRouter()


@router.post("/", response_model=APIResponse[SignalOut])
async def create_signal(
    payload: SignalCreate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    Create a new trading signal (admin-only manual injection).
    The automatic strategy engines write Signal rows directly via the
    database session in strategies/runner.py — they don't call this HTTP
    endpoint. This route exists for manual/admin signal creation, so it's
    admin-gated: unauthenticated signal injection would let anyone push
    fabricated "trading advice" out to every subscriber.
    """
    from datetime import datetime, timedelta, timezone
    from backend.config import settings

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=payload.expiry_minutes or settings.SIGNAL_EXPIRY_MINUTES)

    signal = Signal(
        strategy_type=payload.strategy_type,
        symbol=payload.symbol,
        exchange=payload.exchange,
        asset_class=payload.asset_class,
        direction=payload.direction,
        entry_price=payload.entry_price,
        entry_zone_high=payload.entry_zone_high,
        entry_zone_low=payload.entry_zone_low,
        stop_loss=payload.stop_loss,
        stop_loss_pct=payload.stop_loss_pct,
        risk_reward_ratio=payload.risk_reward_ratio,
        take_profit_levels=payload.take_profit_levels,
        confidence_score=payload.confidence_score,
        ml_metadata=payload.ml_metadata,
        technical_snapshot=payload.technical_snapshot,
        expiry_minutes=payload.expiry_minutes or settings.SIGNAL_EXPIRY_MINUTES,
        expires_at=expires_at,
        status=SignalStatus.ACTIVE,
    )
    db.add(signal)
    await db.flush()
    await db.refresh(signal)

    # TODO: Broadcast to all active users via Telegram
    # from backend.bot.signal_broadcast import broadcast_new_signal
    # await broadcast_new_signal(signal)

    return APIResponse(
        data=SignalOut.model_validate(signal),
        message=f"Signal created: {signal.symbol} {signal.direction.upper()}",
    )


@router.get("/", response_model=APIResponse[PaginatedResponse[SignalOut]])
async def list_signals(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    strategy_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """
    List signals with optional filters.

    ⚠️ FIX: this is the paid product itself and had NO auth at all —
    anyone could scrape every signal for free. Now requires a logged-in
    user with an active subscription, active trial, or admin role.
    """
    if not (user.is_admin or user.is_active or user.trial_active):
        raise HTTPException(
            status_code=403,
            detail="An active subscription or trial is required to view signals.",
        )
    query = select(Signal)

    if status:
        query = query.where(Signal.status == status)
    if strategy_type:
        query = query.where(Signal.strategy_type == strategy_type)

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Paginate
    query = query.order_by(Signal.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    signals = result.scalars().all()

    items = [SignalOut.model_validate(s) for s in signals]

    return APIResponse(
        data=PaginatedResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size,
        )
    )


@router.get("/active", response_model=APIResponse[list[SignalOut]])
async def get_active_signals(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """Get all currently active signals (for real-time display). Same
    subscription-gating as list_signals — this is paid content."""
    if not (user.is_admin or user.is_active or user.trial_active):
        raise HTTPException(
            status_code=403,
            detail="An active subscription or trial is required to view signals.",
        )
    result = await db.execute(
        select(Signal)
        .where(Signal.status == SignalStatus.ACTIVE)
        .order_by(Signal.created_at.desc())
    )
    signals = result.scalars().all()
    return APIResponse(
        data=[SignalOut.model_validate(s) for s in signals],
        message=f"{len(signals)} active signals",
    )
