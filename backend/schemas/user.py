"""
SmAttaker — User Schemas
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field


class UserLoginRequest(BaseModel):
    """Telegram login data for authentication."""
    telegram_id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    language_code: Optional[str] = "en"
    # Telegram Login Widget verification fields (see validate_telegram_hash).
    # Required unless the call comes from a trusted internal service.
    auth_date: Optional[int] = None
    hash: Optional[str] = None
    photo_url: Optional[str] = None


class UserCreate(BaseModel):
    """Create a new user (from Telegram)."""
    telegram_id: int
    telegram_username: Optional[str] = None
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    language: str = "en"


class UserUpdate(BaseModel):
    """Update user fields."""
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    language: Optional[str] = None
    default_account_type: Optional[str] = None
    notes: Optional[str] = None


class UserOut(BaseModel):
    """Public user representation."""
    id: str
    telegram_id: int
    telegram_username: Optional[str] = None
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: str
    status: str
    language: str
    trial_start: Optional[datetime] = None
    trial_end: Optional[datetime] = None
    approved_by_admin: bool
    default_account_type: str
    created_at: datetime

    class Config:
        from_attributes = True


class UserAdminOut(UserOut):
    """Extended user info for admin panel."""
    total_trades: int = 0
    active_subscription: bool = False
    subscription_end: Optional[datetime] = None


class TrialRequest(BaseModel):
    """User requests a free trial."""
    email: EmailStr
    telegram_id: int


class TrialApproval(BaseModel):
    """Admin approves/rejects a trial."""
    user_id: str
    approved: bool
    reason: Optional[str] = None
