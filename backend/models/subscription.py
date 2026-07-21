"""
SmAttaker — Subscription Model
"""
import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.models.base import BaseModel

if TYPE_CHECKING:
    from backend.models.user import User


class Subscription(BaseModel):
    __tablename__ = "subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # ── Plan ────────────────────────────────────────────
    plan_type: Mapped[str] = mapped_column(
        String(32), default="monthly", nullable=False
    )  # trial | monthly | lifetime
    amount_usd: Mapped[float] = mapped_column(Float, default=99.0, nullable=False)

    # ── Payment ─────────────────────────────────────────
    payment_method: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # stripe | crypto
    payment_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )  # pending | paid | expired | cancelled | refunded

    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, nullable=True
    )
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    crypto_tx_hash: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    crypto_currency: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    crypto_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Dates ───────────────────────────────────────────
    start_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relations ────────────────────────────────────────
    user: Mapped["User"] = relationship("User", back_populates="subscriptions")

    @property
    def is_active(self) -> bool:
        if self.payment_status not in ("paid", "trial_active"):
            return False
        if not self.end_date:
            return True
        from backend.models.base import utcnow
        return utcnow() < self.end_date

    def __repr__(self) -> str:
        return f"<Subscription {self.plan_type} {self.payment_status}>"
