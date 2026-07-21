"""
SmAttaker — User Model
"""
import uuid
from datetime import datetime, timezone
from typing import Optional, List, TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, Enum as SAEnum, BigInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.models.base import BaseModel, utcnow

if TYPE_CHECKING:
    from backend.models.subscription import Subscription
    from backend.models.trade import Trade
    from backend.models.exchange_connection import ExchangeConnection
    from backend.models.risk_settings import RiskSettings


class UserRole:
    ADMIN = "admin"
    USER = "user"


class UserStatus:
    ACTIVE = "active"
    INACTIVE = "inactive"
    BANNED = "banned"
    TRIAL = "trial"
    PENDING_APPROVAL = "pending_approval"


class User(BaseModel):
    __tablename__ = "users"

    # ── Telegram Identity ───────────────────────────────
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    telegram_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # ── Account Info ─────────────────────────────────────
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default=UserRole.USER, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=UserStatus.PENDING_APPROVAL, nullable=False, index=True
    )

    # ── Trial ────────────────────────────────────────────
    trial_start: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Refresh Token Rotation ────────────────────────────
    # The jti of the ONE currently-valid refresh token for this user.
    # Set on login and on every successful /api/auth/refresh call;
    # a refresh request presenting a token whose jti doesn't match this
    # is either stale (already rotated out) or stolen — rejected either
    # way, and NULL'd out to force a fresh login rather than silently
    # trusting a token that shouldn't still work.
    current_refresh_jti: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ── Language ─────────────────────────────────────────
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)

    # ── Trading Profile ──────────────────────────────────
    default_account_type: Mapped[str] = mapped_column(
        String(16), default="demo", nullable=False
    )  # "demo" | "real"

    # ── Forgot / ban reason ──────────────────────────────
    notes: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    # ── Relations ────────────────────────────────────────
    subscriptions: Mapped[List["Subscription"]] = relationship(
        "Subscription", back_populates="user", lazy="selectin"
    )
    trades: Mapped[List["Trade"]] = relationship(
        "Trade", back_populates="user", lazy="selectin"
    )
    exchange_connections: Mapped[List["ExchangeConnection"]] = relationship(
        "ExchangeConnection", back_populates="user", lazy="selectin"
    )
    risk_settings: Mapped[List["RiskSettings"]] = relationship(
        "RiskSettings", back_populates="user", lazy="selectin"
    )

    # ── Properties ───────────────────────────────────────
    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE

    @property
    def is_banned(self) -> bool:
        return self.status == UserStatus.BANNED

    @property
    def trial_active(self) -> bool:
        """Check if trial is currently active."""
        if not self.trial_start or not self.trial_end:
            return False
        now = utcnow()
        return self.trial_start <= now <= self.trial_end

    @property
    def trial_expired(self) -> bool:
        if not self.trial_end:
            return False
        return utcnow() > self.trial_end

    def __repr__(self) -> str:
        return f"<User {self.telegram_username or self.telegram_id} ({self.status})>"
