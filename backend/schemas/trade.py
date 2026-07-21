"""
SmAttaker — Trade Schemas
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class TradeCreate(BaseModel):
    """Create a trade from a signal."""
    signal_id: Optional[str] = None
    account_type: str = "demo"  # demo | real
    symbol: str
    exchange: Optional[str] = None
    direction: str  # long | short
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit_levels: Optional[list[dict]] = None
    position_size: float
    position_size_usd: Optional[float] = None
    leverage: int = 1
    risk_percent: float = 0
    strategy: str
    asset_class: str


class TradeUpdate(BaseModel):
    """Update a trade (e.g. close, modify exit)."""
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    status: Optional[str] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    r_multiple: Optional[float] = None
    is_winner: Optional[bool] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None


class TradeOut(BaseModel):
    """Full trade representation."""
    id: str
    user_id: str
    signal_id: Optional[str] = None
    account_type: str
    symbol: str
    exchange: Optional[str] = None
    strategy: str
    asset_class: str
    direction: str
    order_type: str = "market"
    entry_price: float
    entry_time: datetime
    stop_loss: float
    stop_loss_pct: float = 0
    trailing_stop: bool = False
    trailing_distance_pct: Optional[float] = None
    take_profit_levels: Optional[list[dict]] = None
    position_size: float
    position_size_usd: Optional[float] = None
    leverage: int = 1
    risk_percent: float = 0
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    fees: float = 0
    r_multiple: Optional[float] = None
    is_winner: Optional[bool] = None
    status: str = "active"
    notes: Optional[str] = None
    tags: Optional[list] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TradeSummary(BaseModel):
    """Summary stats for a set of trades."""
    total_trades: int = 0
    active_trades: int = 0
    completed_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_pnl_usd: float = 0.0
    avg_r: float = 0.0
    profit_factor: float = 0.0
    best_trade_pnl_pct: float = 0.0
    worst_trade_pnl_pct: float = 0.0
    max_win_streak: int = 0
    max_loss_streak: int = 0


class TradeListResponse(BaseModel):
    """Paginated trade list."""
    trades: list[TradeOut]
    total: int
    summary: Optional[TradeSummary] = None
