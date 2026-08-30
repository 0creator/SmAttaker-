"""
SmAttaker — Admin Notification Model
Alerts for admin: new registrations, payments, errors, etc.
"""
import uuid
from typing import Optional
from sqlalchemy import String, Boolean, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from backend.models.base import BaseModel


class NotificationType:
    NEW_REGISTRATION = "new_registration"
    TRIAL_REQUEST = "trial_request"
    NEW_PAYMENT = "new_payment"
    PAYMENT_FAILED = "payment_failed"
    SUBSCRIPTION_EXPIRED = "subscription_expired"
    SYSTEM_ERROR = "system_error"
    SIGNAL_FAILED = "signal_failed"
    EXCHANGE_ERROR = "exchange_error"
    USER_BANNED = "user_banned"


class AdminNotification(BaseModel):
    __tablename__ = "admin_notifications"

    notification_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), default="info", nullable=False
    )  # info | warning | critical

    # ── Related Entities ────────────────────────────────
    related_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    related_subscription_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # ── Status ──────────────────────────────────────────
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ── Extra Data ──────────────────────────────────────
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<AdminNotification {self.notification_type} [{self.severity}]>"
