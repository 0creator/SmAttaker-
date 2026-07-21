"""
SmAttaker — Analytics API Routes
Institutional-grade analytics: Sharpe, EV, Equity Curve, Rankings, R-Heatmap.
"""
import math
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from backend.database import get_db
from backend.models.trade import Trade, TradeStatus
from backend.models.user import User
from backend.schemas.analytics import (
    AnalyticsSummary, AnalyticsDashboard,
    EquityCurvePoint, InstrumentRanking, RHeatmapData, RHeatmapCell,
)
from backend.schemas.common import APIResponse
from backend.api.auth import get_current_user_dep
from backend.utils.cache import cached_json

router = APIRouter()

# Risk-free rate (annual) for Sharpe ratio calculations
RISK_FREE_RATE = 0.05  # 5%


@router.get("/dashboard", response_model=APIResponse[AnalyticsDashboard])
async def get_analytics_dashboard(
    user_id: Optional[str] = None,
    account_type: Optional[str] = None,
    period_days: int = Query(90, ge=7, le=3650),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """
    Full analytics dashboard:
    - Summary metrics (Sharpe, EV, Win Rate, etc.)
    - Equity curve
    - Top instrument rankings
    - R-Heatmap

    ⚠️ FIX: had no authentication at all — any caller could pass any
    `user_id` and see another user's private trading performance. Now
    non-admins are always scoped to their own id regardless of what's
    passed; admins (or no user_id at all, for the admin panel's
    platform-wide view) keep full visibility.

    ⚠️ PERFORMANCE: this recomputes Sharpe/Sortino/equity-curve/rankings/
    heatmap from every matching trade on every single call — expensive,
    and was hit repeatedly by the admin panel + user dashboard with no
    caching at all. Now cached in Redis for 60 seconds per unique
    (user_id, account_type, period_days) combination — comfortably fresh
    for a dashboard, and collapses bursts of repeated requests (e.g. a
    user tabbing back and forth) into one real computation.
    """
    effective_user_id = user_id
    if user.role != "admin":
        effective_user_id = str(user.id)

    cache_key = f"analytics:dashboard:{effective_user_id}:{account_type}:{period_days}"

    async def _compute():
        cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
        query = select(Trade).where(
            Trade.status == TradeStatus.COMPLETED,
            Trade.exit_time >= cutoff,
        )
        if effective_user_id:
            query = query.where(Trade.user_id == effective_user_id)
        if account_type:
            query = query.where(Trade.account_type == account_type)

        result = await db.execute(query.order_by(Trade.exit_time.asc()))
        trades = list(result.scalars().all())

        summary = _compute_analytics_summary(trades)
        equity_curve = _compute_equity_curve(trades)
        rankings = _compute_instrument_rankings(trades)
        r_heatmap = _compute_r_heatmap(trades)

        dashboard = AnalyticsDashboard(
            summary=summary,
            top_instruments=rankings[:10],
            r_heatmap=r_heatmap,
            equity_curve=equity_curve,
        )
        return dashboard.model_dump(mode="json")

    data = await cached_json(cache_key, ttl_seconds=60, compute_fn=_compute)
    return APIResponse(data=AnalyticsDashboard(**data))


@router.get("/summary", response_model=APIResponse[AnalyticsSummary])
async def get_analytics_summary(
    user_id: Optional[str] = None,
    account_type: Optional[str] = None,
    period_days: int = Query(90, ge=7, le=3650),
    db: AsyncSession = Depends(get_db),
):
    """Get analytics summary only."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
    query = select(Trade).where(
        Trade.status == TradeStatus.COMPLETED,
        Trade.exit_time >= cutoff,
    )
    if user_id:
        query = query.where(Trade.user_id == user_id)
    if account_type:
        query = query.where(Trade.account_type == account_type)

    result = await db.execute(query)
    trades = list(result.scalars().all())
    return APIResponse(data=_compute_analytics_summary(trades))


@router.get("/rankings", response_model=APIResponse[list[InstrumentRanking]])
async def get_instrument_rankings(
    user_id: Optional[str] = None,
    account_type: Optional[str] = None,
    top_n: int = Query(20, ge=5, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Get instrument performance rankings."""
    query = select(Trade).where(Trade.status == TradeStatus.COMPLETED)
    if user_id:
        query = query.where(Trade.user_id == user_id)
    if account_type:
        query = query.where(Trade.account_type == account_type)

    result = await db.execute(query)
    trades = list(result.scalars().all())
    rankings = _compute_instrument_rankings(trades)
    return APIResponse(data=rankings[:top_n])


def _compute_analytics_summary(trades: list[Trade]) -> AnalyticsSummary:
    """Compute comprehensive analytics from a list of completed trades."""
    if not trades:
        return AnalyticsSummary()

    completed = [t for t in trades if t.exit_price and t.status == TradeStatus.COMPLETED]
    winners = [t for t in completed if t.is_winner]
    losers = [t for t in completed if t.is_winner is False]
    n = len(completed)
    n_wins = len(winners)

    # Win Rate
    win_rate = n_wins / n * 100 if n > 0 else 0

    # R-multiples
    r_values = [t.r_multiple or 0 for t in completed]
    avg_r = sum(r_values) / n if n > 0 else 0
    ev = avg_r  # Expected value in R

    # Profit Factor
    gross_profit = sum((t.pnl or 0) for t in winners)
    gross_loss = abs(sum((t.pnl or 0) for t in losers))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Sharpe Ratio (monthly)
    # Group returns by month
    returns = [t.pnl_percent or 0 for t in completed]
    avg_return = sum(returns) / n if n > 0 else 0
    variance = sum((r - avg_return) ** 2 for r in returns) / n if n > 0 else 0
    std_dev = math.sqrt(variance) if variance > 0 else 0
    # Annualized Sharpe (assuming daily returns, ~252 trading days for crypto it's 365)
    daily_rf = RISK_FREE_RATE / 365
    sharpe = ((avg_return / 100 - daily_rf) / (std_dev / 100) * math.sqrt(365)) if std_dev > 0 else 0

    # Sortino Ratio (downside deviation only)
    downside_returns = [r for r in returns if r < 0]
    downside_var = sum(r**2 for r in downside_returns) / n if n > 0 else 0
    downside_dev = math.sqrt(downside_var)
    sortino = ((avg_return / 100 - daily_rf) / (downside_dev / 100) * math.sqrt(365)) if downside_dev > 0 else 0

    # Max Drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for r in returns:
        cumulative += r
        peak = max(peak, cumulative)
        dd = (peak - cumulative) if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Streaks
    max_win_streak = max_loss_streak = curr_streak = 0
    curr_streak_type = ""
    for t in completed:
        if t.is_winner:
            if curr_streak_type == "win":
                curr_streak += 1
            else:
                curr_streak = 1
                curr_streak_type = "win"
            max_win_streak = max(max_win_streak, curr_streak)
        elif t.is_winner is False:
            if curr_streak_type == "loss":
                curr_streak += 1
            else:
                curr_streak = 1
                curr_streak_type = "loss"
            max_loss_streak = max(max_loss_streak, curr_streak)

    # Average win/loss
    avg_win = sum(t.pnl_percent or 0 for t in winners) / n_wins if n_wins > 0 else 0
    avg_loss = sum(t.pnl_percent or 0 for t in losers) / (n - n_wins) if (n - n_wins) > 0 else 0

    # Total return
    initial_balance = 10000  # default; should come from account settings
    total_return_pct = sum(returns)

    # Monthly stats
    monthly_returns = _group_by_month(completed)
    monthly_values = [v for v in monthly_returns.values()]
    profitable_months = sum(1 for v in monthly_values if v > 0)
    avg_monthly = sum(monthly_values) / len(monthly_values) if monthly_values else 0

    return AnalyticsSummary(
        initial_balance=initial_balance,
        current_balance=initial_balance * (1 + total_return_pct / 100),
        total_return=total_return_pct,
        total_return_usd=initial_balance * total_return_pct / 100,
        total_trades=n,
        winning_trades=n_wins,
        losing_trades=n - n_wins,
        win_rate=win_rate,
        profit_factor=pf if pf != float("inf") else 999.99,
        expected_value=ev,
        average_r=avg_r,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown_pct=max_dd,
        max_drawdown_usd=initial_balance * max_dd / 100,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        current_streak=curr_streak,
        current_streak_type=curr_streak_type,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        best_trade_pct=max(returns) if returns else 0,
        worst_trade_pct=min(returns) if returns else 0,
        avg_monthly_return=avg_monthly,
        best_month_pct=max(monthly_values) if monthly_values else 0,
        worst_month_pct=min(monthly_values) if monthly_values else 0,
        profitable_months_pct=profitable_months / len(monthly_values) * 100 if monthly_values else 0,
        equity_curve=_compute_equity_curve(trades),
    )


def _compute_equity_curve(trades: list[Trade]) -> list[EquityCurvePoint]:
    """Build equity curve from trades sorted by time."""
    sorted_trades = sorted(trades, key=lambda t: t.exit_time or t.entry_time)
    equity = 10000  # starting balance
    peak = equity
    curve = []

    # Add starting point
    curve.append(EquityCurvePoint(
        date=(sorted_trades[0].entry_time if sorted_trades else datetime.now(timezone.utc)).isoformat(),
        equity=equity,
        pnl=0,
        pnl_pct=0,
        drawdown_pct=0,
    ))

    for t in sorted_trades:
        pnl_pct = t.pnl_percent or 0
        equity_change = equity * pnl_pct / 100
        equity += equity_change
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100 if peak > 0 else 0

        curve.append(EquityCurvePoint(
            date=(t.exit_time or t.entry_time).isoformat(),
            equity=round(equity, 2),
            pnl=round(equity_change, 2),
            pnl_pct=round(pnl_pct, 4),
            drawdown_pct=round(dd, 2),
        ))

    return curve


def _compute_instrument_rankings(trades: list[Trade]) -> list[InstrumentRanking]:
    """Rank instruments by performance."""
    from collections import defaultdict
    groups = defaultdict(list)
    for t in trades:
        groups[t.symbol].append(t)

    rankings = []
    for symbol, sym_trades in groups.items():
        completed = [t for t in sym_trades if t.status == TradeStatus.COMPLETED]
        winners = [t for t in completed if t.is_winner]
        losers = [t for t in completed if t.is_winner is False]
        n = len(completed)
        n_wins = len(winners)

        gross_profit = sum((t.pnl or 0) for t in winners)
        gross_loss = abs(sum((t.pnl or 0) for t in losers))
        pf = gross_profit / gross_loss if gross_loss > 0 else (0 if n == 0 else 999)

        # Streaks
        max_ws = max_ls = curr = 0
        curr_type = ""
        for t in sorted(completed, key=lambda x: x.exit_time or x.created_at):
            if t.is_winner:
                curr = curr + 1 if curr_type == "win" else 1
                curr_type = "win"
                max_ws = max(max_ws, curr)
            elif t.is_winner is False:
                curr = curr + 1 if curr_type == "loss" else 1
                curr_type = "loss"
                max_ls = max(max_ls, curr)

        rankings.append(InstrumentRanking(
            symbol=symbol,
            asset_class=sym_trades[0].asset_class if sym_trades else "unknown",
            total_trades=n,
            winning_trades=n_wins,
            losing_trades=n - n_wins,
            win_rate=n_wins / n * 100 if n > 0 else 0,
            profit_factor=pf,
            total_pnl_pct=sum(t.pnl_percent or 0 for t in completed),
            avg_r=sum(t.r_multiple or 0 for t in completed) / n if n > 0 else 0,
            max_win_streak=max_ws,
            max_loss_streak=max_ls,
            best_trade_pct=max((t.pnl_percent or 0) for t in completed) if completed else 0,
            worst_trade_pct=min((t.pnl_percent or 0) for t in completed) if completed else 0,
        ))

    # Sort by profit factor (desc)
    rankings.sort(key=lambda r: (r.win_rate * r.profit_factor if r.total_trades >= 3 else 0), reverse=True)
    for i, r in enumerate(rankings):
        r.rank = i + 1

    return rankings


def _compute_r_heatmap(trades: list[Trade]) -> Optional[RHeatmapData]:
    """Generate R-heatmap data (monthly)."""
    from collections import defaultdict
    monthly = defaultdict(lambda: {"r_sum": 0.0, "count": 0})
    for t in trades:
        if t.exit_time and t.r_multiple is not None:
            month_key = t.exit_time.strftime("%Y-%m")
            monthly[month_key]["r_sum"] += t.r_multiple
            monthly[month_key]["count"] += 1

    if not monthly:
        return None

    cells = [
        RHeatmapCell(
            period=month,
            r_value=round(data["r_sum"] / data["count"], 2) if data["count"] > 0 else 0,
            trades_count=data["count"],
        )
        for month, data in sorted(monthly.items())
    ]
    return RHeatmapData(cells=cells, period_type="monthly")


def _group_by_month(trades: list[Trade]) -> dict:
    """Group trade P&L by month."""
    from collections import defaultdict
    monthly = defaultdict(float)
    for t in trades:
        if t.exit_time:
            key = t.exit_time.strftime("%Y-%m")
            monthly[key] += t.pnl_percent or 0
    return dict(monthly)
