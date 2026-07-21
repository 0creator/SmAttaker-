"""
SmAttaker — Risk Settings Schemas
"""
from typing import Optional
from pydantic import BaseModel, Field


class RiskSettingsCreate(BaseModel):
    """Create risk settings for an account type."""
    account_type: str = "demo"
    name: str = "Default Risk Profile"
    max_risk_per_trade_pct: float = 1.0
    max_daily_risk_pct: float = 3.0
    max_open_positions: int = 3
    max_leverage: int = 10
    position_sizing_method: str = "risk_based"
    fixed_position_size: float = 100.0
    risk_reward_min_ratio: float = 1.5
    stop_loss_type: str = "fixed"
    take_profit_strategy: str = "partial"
    is_active: bool = True


class RiskSettingsUpdate(BaseModel):
    """Update any risk setting field."""
    name: Optional[str] = None
    max_risk_per_trade_pct: Optional[float] = None
    max_daily_risk_pct: Optional[float] = None
    max_weekly_risk_pct: Optional[float] = None
    max_monthly_risk_pct: Optional[float] = None
    max_open_positions: Optional[int] = None
    max_leverage: Optional[int] = None
    position_sizing_method: Optional[str] = None
    fixed_position_size: Optional[float] = None
    risk_reward_min_ratio: Optional[float] = None
    stop_loss_type: Optional[str] = None
    take_profit_strategy: Optional[str] = None
    allowed_symbols: Optional[list[str]] = None
    blocked_symbols: Optional[list[str]] = None
    allowed_asset_classes: Optional[list[str]] = None
    is_active: Optional[bool] = None


class RiskSettingsOut(BaseModel):
    """Full risk settings representation."""
    id: str
    user_id: str
    account_type: str
    name: str
    max_risk_per_trade_pct: float
    max_daily_risk_pct: float
    max_weekly_risk_pct: float
    max_monthly_risk_pct: float
    max_open_positions: int
    max_leverage: int
    position_sizing_method: str
    fixed_position_size: float
    risk_reward_min_ratio: float
    stop_loss_type: str
    trailing_stop_activation_pct: float
    trailing_stop_distance_pct: float
    take_profit_strategy: str
    tp1_pct: float
    tp2_pct: float
    tp3_pct: float
    allowed_symbols: Optional[list] = None
    blocked_symbols: Optional[list] = None
    allowed_asset_classes: Optional[list] = None
    daily_loss_lock_enabled: bool
    is_active: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True
