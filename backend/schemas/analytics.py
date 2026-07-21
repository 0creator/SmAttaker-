"""
SmAttaker — Analytics Schemas
Comprehensive analytics output for the Analysis section.
"""
from typing import Optional
from pydantic import BaseModel


class EquityCurvePoint(BaseModel):
    """Single point on the equity curve."""
    date: str  # ISO 8601
    equity: float
    pnl: float = 0.0
    pnl_pct: float = 0.0
    drawdown_pct: float = 0.0


class AnalyticsSummary(BaseModel):
    """Complete analytics snapshot for a portfolio."""
    # ── Account ────────────────────────────────────────
    initial_balance: float = 0.0
    current_balance: float = 0.0
    total_return: float = 0.0  # %
    total_return_usd: float = 0.0

    # ── Trade Stats ─────────────────────────────────────
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0  # 0-100%

    # ── Advanced Metrics ────────────────────────────────
    profit_factor: float = 0.0
    expected_value: float = 0.0  # EV per trade (R)
    average_r: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0

    # ── Streaks ─────────────────────────────────────────
    max_win_streak: int = 0
    max_loss_streak: int = 0
    current_streak: int = 0
    current_streak_type: str = ""  # win | loss

    # ── Trade Metrics ───────────────────────────────────
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    avg_win_hold_hours: float = 0.0
    avg_loss_hold_hours: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0

    # ── Monthly / Period ────────────────────────────────
    avg_monthly_return: float = 0.0
    best_month_pct: float = 0.0
    worst_month_pct: float = 0.0
    profitable_months_pct: float = 0.0

    # ── Equity Curve ────────────────────────────────────
    equity_curve: list[EquityCurvePoint] = []


class InstrumentRanking(BaseModel):
    """Per-instrument performance ranking."""
    symbol: str
    asset_class: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_pnl_pct: float = 0.0
    avg_r: float = 0.0
    max_win_streak: int = 0
    max_loss_streak: int = 0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    rank: int = 0  # position in ranking


class RHeatmapCell(BaseModel):
    """Single cell in the R-heatmap."""
    period: str  # "2026-07" or "2026-W27"
    r_value: float
    trades_count: int = 0


class RHeatmapData(BaseModel):
    """R-Heatmap data for visualization."""
    cells: list[RHeatmapCell]
    period_type: str = "monthly"  # monthly | weekly


class AnalyticsDashboard(BaseModel):
    """Full analytics dashboard — the complete package."""
    summary: AnalyticsSummary
    top_instruments: list[InstrumentRanking]  # top 10 ranked
    r_heatmap: Optional[RHeatmapData] = None
    equity_curve: list[EquityCurvePoint] = []
