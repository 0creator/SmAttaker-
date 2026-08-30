"""
SmAttaker — Subscription Schemas
"""
import uuid
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


class SubscriptionCreate(BaseModel):
    """Create a subscription."""
    plan_type: str = "monthly"  # trial | monthly | lifetime
    payment_method: str  # stripe | crypto
    amount_usd: float = 99.0


class PaymentVerify(BaseModel):
    """Verify crypto payment via TX hash."""
    tx_hash: str
    currency: str = "USDT"
    amount: float


class SubscriptionOut(BaseModel):
    """Subscription representation."""
    id: str
    user_id: str
    plan_type: str
    amount_usd: float
    payment_method: str
    payment_status: str
    start_date: datetime
    end_date: Optional[datetime] = None
    auto_renew: bool = True
    is_active: bool = False
    created_at: datetime

    class Config:
        from_attributes = True

    @field_validator("id", "user_id", mode="before")
    @classmethod
    def _coerce_uuid_to_str(cls, v: Any) -> str:
        """Coerce uuid.UUID → str. Pydantic v2 strict mode refuses to
        auto-coerce, and SQLAlchemy returns a UUID object for UUID columns."""
        if v is None:
            return ""
        if isinstance(v, uuid.UUID):
            return str(v)
        return str(v)
