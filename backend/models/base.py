"""
SmAttaker — Shared Model Mixins
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class TimestampMixin:
    """Adds created_at / updated_at columns automatically."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class BaseModel(Base, TimestampMixin):
    """Base for all models — UUID primary key + timestamps."""
    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
