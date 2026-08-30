"""
SmAttaker — Trades API Routes
Trade journal: list, filter, create, update, close.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from typing import Optional

from backend.database import get_db
from backend.models.trade import Trade, TradeStatus
from backend.models.user import User
from backend.schemas.trade import TradeOut, TradeCreate, TradeUpdate, TradeSummary, TradeListResponse
from backend.schemas.common import APIResponse
from backend.api.auth import get_current_user_dep, require_admin

router = APIRouter()


@router.post("/", response_model=APIResponse[TradeOut])
async def create_trade(
    payload: TradeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """
    Open a new trade (from signal or manual) for the authenticated user.

    ⚠️ FIX: `Trade.user_id` is a required (NOT NULL) column, and the
    `TradeCreate` request schema never even had a `user_id` field — this
    endpoint was unconditionally broken and would fail on every call
    with an integrity error the moment it tried to insert. It's now tied
    to the authenticated caller's own id (never trust a client-supplied
    user_id here — that would let user A open trades "as" user B).

    When `signal_id` is provided, we also auto-populate take_profit_levels
    and stop_loss_pct from the signal — the client doesn't have to send
    them, and the signal monitor needs them to detect TP hits.
    """
    # If a signal_id was provided, fetch the signal to fill in missing
    # fields (take_profit_levels, stop_loss_pct). The signal monitor
    # reads take_profit_levels from the SIGNAL anyway, but having them
    # on the trade too makes the trade self-contained for display.
    take_profit_levels = payload.take_profit_levels
    stop_loss_pct = 0.0
    if payload.signal_id:
        from backend.models.signal import Signal
        sig_result = await db.execute(select(Signal).where(Signal.id == payload.signal_id))
        sig = sig_result.scalar_one_or_none()
        if sig is not None:
            # Inherit TP levels from the signal if the client didn't send them
            if take_profit_levels is None:
                take_profit_levels = sig.take_profit_levels
            # Compute stop_loss_pct from signal if available
            if sig.stop_loss_pct:
                stop_loss_pct = float(sig.stop_loss_pct)
            else:
                # Compute from entry + SL
                if payload.entry_price and payload.stop_loss:
                    if payload.direction == "long":
                        stop_loss_pct = abs((payload.entry_price - payload.stop_loss) / payload.entry_price * 100)
                    else:
                        stop_loss_pct = abs((payload.stop_loss - payload.entry_price) / payload.entry_price * 100)

    trade = Trade(
        user_id=user.id,
        signal_id=payload.signal_id,
        account_type=payload.account_type,
        symbol=payload.symbol,
        exchange=payload.exchange,
        direction=payload.direction,
        entry_price=payload.entry_price,
        entry_time=payload.entry_time,
        stop_loss=payload.stop_loss,
        stop_loss_pct=stop_loss_pct,
        take_profit_levels=take_profit_levels,
        position_size=payload.position_size,
        position_size_usd=payload.position_size_usd,
        leverage=payload.leverage,
        risk_percent=payload.risk_percent,
        strategy=payload.strategy,
        asset_class=payload.asset_class,
        status=TradeStatus.ACTIVE,
    )
    # Calculate stop loss % if not already set from the signal
    if not trade.stop_loss_pct:
        if payload.direction == "long":
            trade.stop_loss_pct = abs((payload.entry_price - payload.stop_loss) / payload.entry_price * 100)
        else:
            trade.stop_loss_pct = abs((payload.stop_loss - payload.entry_price) / payload.entry_price * 100)

    db.add(trade)
    await db.flush()
    await db.refresh(trade)

    return APIResponse(
        data=TradeOut.model_validate(trade),
        message=f"Trade opened: {trade.symbol} {trade.direction.upper()}",
    )


@router.get("/", response_model=APIResponse[TradeListResponse])
async def list_trades(
    user_id: Optional[str] = None,
    account_type: Optional[str] = None,
    status: Optional[str] = None,
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    asset_class: Optional[str] = None,
    direction: Optional[str] = None,
    is_winner: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """
    List trades with powerful filtering — the Trading Journal.

    ⚠️ FIX: this endpoint had no authentication at all, and would happily
    return ANY user's trade history for a client-supplied `user_id` —
    a straightforward privacy leak (position sizes, win/loss history,
    exchange used). Non-admins can now only ever see their OWN trades;
    the `user_id` query param is ignored for them (silently forced to
    their own id) rather than trusting client input. Admins keep full
    visibility for support/oversight.
    """
    query = select(Trade)

    if user.role == "admin":
        if user_id:
            query = query.where(Trade.user_id == user_id)
    else:
        query = query.where(Trade.user_id == user.id)
    if account_type:
        query = query.where(Trade.account_type == account_type)
    if status:
        query = query.where(Trade.status == status)
    if symbol:
        query = query.where(Trade.symbol.ilike(f"%{symbol}%"))
    if strategy:
        query = query.where(Trade.strategy == strategy)
    if asset_class:
        query = query.where(Trade.asset_class == asset_class)
    if direction:
        query = query.where(Trade.direction == direction)
    if is_winner is not None:
        query = query.where(Trade.is_winner == is_winner)

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Paginate
    query = query.order_by(Trade.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    trades = result.scalars().all()

    items = [TradeOut.model_validate(t) for t in trades]

    # Summary for filtered set
    summary = _compute_trade_summary(trades)

    return APIResponse(
        data=TradeListResponse(trades=items, total=total, summary=summary)
    )


@router.get("/active", response_model=APIResponse[list[TradeOut]])
async def get_active_trades(
    user_id: Optional[str] = None,
    account_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """Get all active (open) trades. Same ownership rule as list_trades."""
    query = select(Trade).where(Trade.status == TradeStatus.ACTIVE)
    if user.role == "admin":
        if user_id:
            query = query.where(Trade.user_id == user_id)
    else:
        query = query.where(Trade.user_id == user.id)
    if account_type:
        query = query.where(Trade.account_type == account_type)

    result = await db.execute(query.order_by(Trade.entry_time.desc()))
    trades = result.scalars().all()
    return APIResponse(
        data=[TradeOut.model_validate(t) for t in trades],
        message=f"{len(trades)} active trades",
    )


@router.put("/{trade_id}/close", response_model=APIResponse[TradeOut])
async def close_trade(
    trade_id: str,
    payload: TradeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """Close an active trade (manually or via signal resolution). Only the
    trade's owner or an admin may close it."""
    result = await db.execute(select(Trade).where(Trade.id == trade_id))
    trade = result.scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")
    if trade.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="You do not own this trade.")
    if trade.status != TradeStatus.ACTIVE:
        raise HTTPException(status_code=400, detail=f"Trade is already {trade.status}.")

    trade.exit_price = payload.exit_price
    trade.exit_time = payload.exit_time
    trade.exit_reason = payload.exit_reason
    trade.notes = payload.notes
    trade.tags = payload.tags

    # ── P&L ──────────────────────────────────────────────
    # ⚠️ v53 FIX: this used to compute P&L independently, right here,
    # with its own (buggy) formula — it multiplied the price-move % by
    # `trade.leverage` a SECOND time on top of a position_size_usd that
    # trade_executor.py already scales by leverage at trade creation,
    # silently overstating both wins and losses by the leverage factor.
    # It also never set `pnl_usd` (only `pnl`) and never updated
    # paper_balance, so a manually-closed PAPER trade's result never
    # reached the user's paper account. All of that now goes through
    # the same shared close logic signal_monitor.py uses — see
    # backend/services/trade_outcomes.py for the full explanation.
    if trade.exit_price and trade.entry_price:
        from backend.services.trade_outcomes import apply_trade_close
        await apply_trade_close(
            db, trade, float(trade.exit_price), payload.exit_reason or "manual",
            outcome=None, signal=None,
        )
    else:
        trade.status = TradeStatus.COMPLETED

    db.add(trade)
    await db.refresh(trade)
    result_label = (
        f"{'🟢 WIN' if trade.is_winner else '🔴 LOSS'} {trade.pnl_percent:.2f}%"
        if trade.pnl_percent is not None else "closed"
    )
    return APIResponse(
        data=TradeOut.model_validate(trade),
        message=f"Trade closed: {result_label}",
    )


def _compute_trade_summary(trades: list[Trade]) -> TradeSummary:
    """Compute summary stats from a list of trades."""
    if not trades:
        return TradeSummary()

    completed = [t for t in trades if t.status == TradeStatus.COMPLETED]
    winners = [t for t in completed if t.is_winner]
    losers = [t for t in completed if t.is_winner is False]
    active = [t for t in trades if t.status == TradeStatus.ACTIVE]

    # Win streaks
    win_streak = loss_streak = max_win = max_loss = 0
    sorted_trades = sorted(completed, key=lambda t: t.exit_time or t.created_at)
    for t in sorted_trades:
        if t.is_winner:
            win_streak += 1
            loss_streak = 0
            max_win = max(max_win, win_streak)
        elif t.is_winner is False:
            loss_streak += 1
            win_streak = 0
            max_loss = max(max_loss, loss_streak)

    total_pnl = sum((t.pnl or 0) for t in completed)
    total_wins = sum((t.pnl or 0) for t in winners)
    total_losses = abs(sum((t.pnl or 0) for t in losers))

    return TradeSummary(
        total_trades=len(trades),
        active_trades=len(active),
        completed_trades=len(completed),
        winning_trades=len(winners),
        losing_trades=len(losers),
        win_rate=len(winners) / len(completed) * 100 if completed else 0,
        total_pnl=total_pnl,
        profit_factor=total_wins / total_losses if total_losses > 0 else (float("inf") if total_wins > 0 else 0),
        best_trade_pnl_pct=max((t.pnl_percent or 0) for t in completed) if completed else 0,
        worst_trade_pnl_pct=min((t.pnl_percent or 0) for t in completed) if completed else 0,
        max_win_streak=max_win,
        max_loss_streak=max_loss,
        avg_r=sum(t.r_multiple or 0 for t in completed) / len(completed) if completed else 0,
    )
