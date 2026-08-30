"""
SmAttaker — User Engagement Model
====================================
Tracks *process* metrics, deliberately not outcome metrics. This is the
single most important design decision in this file, so it's worth
stating explicitly:

  - The discipline streak rewards staying within your OWN pre-declared
    risk limit (RiskSettings.max_risk_per_trade_pct) — not win rate,
    not profit, not trade frequency. A user who takes zero trades in a
    week keeps their streak. A user who takes one oversized trade
    breaks it, even if that trade won.
  - There is no "trade more to level up" mechanic anywhere in this
    system, no leaderboard ranking users against each other, and no
    badge tied to raw P&L. A trading bot that gamifies volume or profit
    chasing is gamifying exactly the behavior good risk management
    exists to prevent. Everything here is designed to reward the
    opposite: patience, consistency, and staying inside your own rules.

One row per user (one-to-one), created lazily on first evaluation
rather than at signup — a user who never completes a real trade simply
never gets a row, which is the correct state (there's nothing to
measure yet).
"""
import uuid
from datetime import date as date_type, datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Integer, Date, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.models.base import BaseModel

if TYPE_CHECKING:
    from backend.models.user import User


class DigestFrequency:
    OFF = "off"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class UserEngagement(BaseModel):
    __tablename__ = "user_engagement"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    # ── Discipline streak (risk-adherence, not P&L) ────────
    discipline_streak_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discipline_streak_best: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_evaluated_date: Mapped[Optional[date_type]] = mapped_column(Date, nullable=True)
    last_violation_date: Mapped[Optional[date_type]] = mapped_column(Date, nullable=True)

    # ── Badges earned ────────────────────────────────────
    # [{"code": "streak_7", "earned_at": "2026-08-19T00:10:00+00:00"}, ...]
    badges: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True, default=list)

    # ── Digest preferences ───────────────────────────────
    digest_frequency: Mapped[str] = mapped_column(
        String(16), default=DigestFrequency.WEEKLY, nullable=False
    )
    last_digest_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relations ─────────────────────────────────────────
    user: Mapped["User"] = relationship("User", back_populates="engagement")

    def __repr__(self) -> str:
        return f"<UserEngagement user={self.user_id} streak={self.discipline_streak_days}>"
