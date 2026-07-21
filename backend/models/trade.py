"""
SmAttaker — Trade Model (Trading Journal)
Every single trade — demo or real — with exhaustive detail.
"""
import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Float, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.models.base import BaseModel

if TYPE_CHECKING:
    from backend.models.user import User
    from backend.models.signal import Signal


class TradeStatus:
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ExitReason:
    TAKE_PROFIT = "tp"
    STOP_LOSS = "sl"
    TRAILING_STOP = "trailing_stop"
    MANUAL = "manual"
    EXPIRED = "expired"
    LIQUIDATED = "liquidated"
    ERROR = "error"


class Trade(BaseModel):
    __tablename__ = "trades"

    # ── Owner ───────────────────────────────────────────
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    signal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )

    # ── Account ─────────────────────────────────────────
    account_type: Mapped[str] = mapped_column(
        String(16), default="demo", nullable=False, index=True
    )  # demo | real

    # ── Instrument ──────────────────────────────────────
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    strategy: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # crypto_strategy | gold_forex_strategy
    asset_class: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # crypto | forex | gold | stocks

    # ── Direction / Type ────────────────────────────────
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # long | short
    order_type: Mapped[str] = mapped_column(
        String(32), default="market", nullable=False
    )  # market | limit | stop

    # ── Entry ───────────────────────────────────────────
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    entry_order_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # ── Risk Params ─────────────────────────────────────
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    trailing_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    trailing_distance_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Take Profit Levels (JSON) ───────────────────────
    # [{"level": 1, "price": 67200, "pct": 1.5, "size_pct": 50}, ...]
    take_profit_levels: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # ── Position ────────────────────────────────────────
    position_size: Mapped[float] = mapped_column(Float, nullable=False)
    position_size_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    leverage: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    risk_percent: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    risk_amount_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Exit ────────────────────────────────────────────
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    exit_order_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # ── P&L ─────────────────────────────────────────────
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fees: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    fees_currency: Mapped[str] = mapped_column(String(16), default="USD", nullable=False)

    # ── R-Multiple ──────────────────────────────────────
    r_multiple: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Result ──────────────────────────────────────────
    is_winner: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # ── Status ──────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(32), default=TradeStatus.ACTIVE, nullable=False, index=True
    )

    # ── Metadata ────────────────────────────────────────
    raw_signal_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    execution_log: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)  # user tags

    # ── Relations ────────────────────────────────────────
    user: Mapped["User"] = relationship("User", back_populates="trades")
    signal: Mapped[Optional["Signal"]] = relationship("Signal", back_populates="trades")

    def __repr__(self) -> str:
        return f"<Trade {self.symbol} {self.direction} @ {self.entry_price} [{self.status}]>"
