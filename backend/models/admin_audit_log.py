"""
SmAttaker — Admin Audit Log Model
Immutable record of every sensitive admin action: who did what, to
whom, when. This is what an institutional-grade platform needs for
accountability (and for answering "who approved this payment / banned
this user / and when" without having to dig through Render logs).
"""
import uuid
from typing import Optional
from sqlalchemy import String, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from backend.models.base import BaseModel


class AuditAction:
    USER_STATUS_CHANGED = "user_status_changed"
    TRIAL_APPROVED = "trial_approved"
    TRIAL_REJECTED = "trial_rejected"
    PAYMENT_CONFIRMED = "payment_confirmed"
    PAYMENT_REJECTED = "payment_rejected"
    SIGNAL_CREATED_MANUALLY = "signal_created_manually"
    STRATEGY_TRIGGERED_MANUALLY = "strategy_triggered_manually"
    BROADCAST_SENT = "broadcast_sent"
    SUBSCRIPTION_GRANTED = "subscription_granted"


class AdminAuditLog(BaseModel):
    __tablename__ = "admin_audit_logs"

    # Who performed the action (nullable so a log row is never lost even
    # if the admin account is later deleted — ON DELETE SET NULL).
    admin_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    admin_telegram_id: Mapped[Optional[int]] = mapped_column(nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # What the action was performed on (a user, a subscription, a
    # signal...) — generic so one table covers every action type.
    target_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Free-form details: old/new values, reason text, IP, etc.
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<AdminAuditLog {self.action} by {self.admin_telegram_id} on {self.target_type}:{self.target_id}>"
