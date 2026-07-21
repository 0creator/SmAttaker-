"""
SmAttaker — Signal Model (Raw ML Strategy Output)
"""
import uuid
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from sqlalchemy import String, Float, DateTime, Integer, Boolean
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.models.base import BaseModel

if TYPE_CHECKING:
    from backend.models.trade import Trade


class SignalStatus:
    PENDING = "pending"
    ACTIVE = "active"
    EXECUTED = "executed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Signal(BaseModel):
    __tablename__ = "signals"

    # ── Strategy ────────────────────────────────────────
    strategy_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # crypto | gold_forex
    strategy_version: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )  # "v2.3"

    # ── Instrument ──────────────────────────────────────
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    asset_class: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # crypto | forex | gold | stocks

    # ── Direction ───────────────────────────────────────
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # long | short

    # ── Entry ───────────────────────────────────────────
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    entry_zone_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_zone_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Risk ────────────────────────────────────────────
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    risk_reward_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Take Profit ─────────────────────────────────────
    # [{"level": 1, "price": 67200, "pct": 1.5, "size_pct": 50}, ...]
    take_profit_levels: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # ── ML Metadata ─────────────────────────────────────
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ml_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # e.g. {"model": "xgboost_v3", "features": {...}, "probability": 0.87}

    # ── Technical Indicators (at signal time) ───────────
    technical_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # e.g. {"rsi": 32, "macd": "bearish_cross", "ema_50": 66200, ...}

    # ── Signal Lifecycle ────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(32), default=SignalStatus.ACTIVE, nullable=False, index=True
    )
    expiry_minutes: Mapped[int] = mapped_column(
        Integer, default=60, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    broadcast_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    executed_trades_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Result (filled after signal resolves) ───────────
    outcome: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )  # won | lost | expired | cancelled
    outcome_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome_pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Flags ───────────────────────────────────────────
    is_premium_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ── Relations ────────────────────────────────────────
    trades: Mapped[List["Trade"]] = relationship("Trade", back_populates="signal")

    def __repr__(self) -> str:
        return (
            f"<Signal {self.symbol} {self.direction} @ "
            f"{self.entry_price} [{self.confidence_score:.1%}]>"
        )
