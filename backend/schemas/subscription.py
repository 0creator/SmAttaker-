"""
SmAttaker — Subscription Schemas
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


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
