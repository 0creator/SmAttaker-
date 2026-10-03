"""
SmAttaker — Risk Settings Model
Per-user, per-account-type risk management configuration.
Full flexibility — user controls everything.
"""
import uuid
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Float, Boolean, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.models.base import BaseModel

if TYPE_CHECKING:
    from backend.models.user import User


class PositionSizingMethod:
    FIXED = "fixed"
    KELLY = "kelly"
    FRACTIONAL = "fractional"
    RISK_BASED = "risk_based"


class StopLossType:
    FIXED = "fixed"
    ATR = "atr"
    TRAILING = "trailing"
    STRUCTURE = "structure"


class TakeProfitStrategy:
    SINGLE = "single"
    PARTIAL = "partial"
    SCALING = "scaling"


class RiskSettings(BaseModel):
    __tablename__ = "risk_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_type: Mapped[str] = mapped_column(
        String(16), default="demo", nullable=False
    )  # demo | real

    # ── Name/Label ─────────────────────────────────────────
    name: Mapped[str] = mapped_column(
        String(128), default="Default Risk Profile", nullable=False
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ── Risk Limits ────────────────────────────────────────
    max_risk_per_trade_pct: Mapped[float] = mapped_column(
        Float, default=1.0, nullable=False
    )  # 1% of capital per trade
    max_daily_risk_pct: Mapped[float] = mapped_column(
        Float, default=3.0, nullable=False
    )  # 3% daily loss limit
    max_weekly_risk_pct: Mapped[float] = mapped_column(
        Float, default=6.0, nullable=False
    )
    max_monthly_risk_pct: Mapped[float] = mapped_column(
        Float, default=12.0, nullable=False
    )
    max_open_positions: Mapped[int] = mapped_column(
        Integer, default=3, nullable=False
    )
    max_concurrent_same_symbol: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    max_leverage: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

    # ── Position Sizing ────────────────────────────────────
    position_sizing_method: Mapped[str] = mapped_column(
        String(32), default=PositionSizingMethod.RISK_BASED, nullable=False
    )
    fixed_position_size: Mapped[float] = mapped_column(
        Float, default=100.0, nullable=False
    )  # in USD
    kelly_fraction: Mapped[float] = mapped_column(
        Float, default=0.25, nullable=False
    )  # 0.25 = quarter Kelly
    fractional_multiplier: Mapped[float] = mapped_column(
        Float, default=1.0, nullable=False
    )

    # ── Entry Filters ──────────────────────────────────────
    risk_reward_min_ratio: Mapped[float] = mapped_column(
        Float, default=1.5, nullable=False
    )  # minimum RR to take trade
    min_confidence_score: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )

    # ── Stop Loss ──────────────────────────────────────────
    stop_loss_type: Mapped[str] = mapped_column(
        String(32), default=StopLossType.FIXED, nullable=False
    )
    atr_period: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    atr_multiplier: Mapped[float] = mapped_column(
        Float, default=2.0, nullable=False
    )
    trailing_stop_activation_pct: Mapped[float] = mapped_column(
        Float, default=1.0, nullable=False
    )  # activate trailing after 1% profit
    trailing_stop_distance_pct: Mapped[float] = mapped_column(
        Float, default=0.5, nullable=False
    )  # trail 0.5% behind price

    # ── Take Profit ────────────────────────────────────────
    take_profit_strategy: Mapped[str] = mapped_column(
        String(32), default=TakeProfitStrategy.PARTIAL, nullable=False
    )
    tp1_pct: Mapped[float] = mapped_column(Float, default=50.0, nullable=False)
    tp2_pct: Mapped[float] = mapped_column(Float, default=30.0, nullable=False)
    tp3_pct: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)

    # ── Symbol Filters ─────────────────────────────────────
    allowed_symbols: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=True
    )  # [] = all allowed | ["BTC/USDT", "ETH/USDT"]
    blocked_symbols: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=True
    )  # symbols to exclude
    allowed_asset_classes: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=True
    )  # ["crypto", "gold"]

    # ── Time Filters ───────────────────────────────────────
    trading_hours_start_utc: Mapped[Optional[str]] = mapped_column(
        String(5), nullable=True
    )  # "00:00" (null = 24/7)
    trading_hours_end_utc: Mapped[Optional[str]] = mapped_column(
        String(5), nullable=True
    )
    blacklisted_days: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=True
    )  # ["mon","tue"] or [] = none

    # ── Daily Loss Lock ────────────────────────────────────
    daily_loss_lock_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    daily_loss_reset_hour_utc: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    # ── Active ─────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Relations ──────────────────────────────────────────
    user: Mapped["User"] = relationship("User", back_populates="risk_settings")

    def __repr__(self) -> str:
        return f"<RiskSettings {self.name} ({self.account_type})>"
