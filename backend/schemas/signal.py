"""
SmAttaker — Signal Schemas
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class SignalCreate(BaseModel):
    """Create a new trading signal (from strategy engine)."""
    strategy_type: str  # crypto | gold_forex
    symbol: str
    exchange: Optional[str] = None
    asset_class: str  # crypto | forex | gold | stocks
    direction: str  # long | short
    entry_price: float
    entry_zone_high: Optional[float] = None
    entry_zone_low: Optional[float] = None
    stop_loss: float
    stop_loss_pct: float = 0
    risk_reward_ratio: Optional[float] = None
    take_profit_levels: Optional[list[dict]] = None
    confidence_score: Optional[float] = None
    ml_metadata: Optional[dict] = None
    technical_snapshot: Optional[dict] = None
    expiry_minutes: int = 60


class SignalOut(BaseModel):
    """Public signal representation (sent to users)."""
    id: str
    strategy_type: str
    strategy_version: Optional[str] = None
    symbol: str
    exchange: Optional[str] = None
    asset_class: str
    direction: str
    entry_price: float
    entry_zone_high: Optional[float] = None
    entry_zone_low: Optional[float] = None
    stop_loss: float
    stop_loss_pct: float = 0
    risk_reward_ratio: Optional[float] = None
    take_profit_levels: Optional[list[dict]] = None
    confidence_score: Optional[float] = None
    technical_snapshot: Optional[dict] = None
    status: str = "active"
    expires_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True
