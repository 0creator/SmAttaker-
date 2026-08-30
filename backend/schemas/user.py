"""
SmAttaker — User Schemas

⚠️ v45 ROBUSTNESS FIX:
UserOut previously declared `paper_balance`, `paper_initial_balance`, and
`onboarding_completed` as NON-Optional with hardcoded defaults. That broke
on every user row that predated the paper-trading migration (0003) — those
columns were physically NULL in the database, so `UserOut.model_validate(u)`
threw a 4-field ValidationError on every admin panel load. This cascaded
into the entire admin panel showing "Loading…" forever because the /api/users
endpoint 500'd before any data reached the browser.

The fix: declare those fields as Optional with safe defaults, and add a
field validator that coerces UUID → str on `id` (the database returns a
uuid.UUID object, but Pydantic v2 in strict mode refuses to coerce it).
"""
import uuid
from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, EmailStr, Field, field_validator


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
    """Public user representation.

    All fields that originate from a database column added after launch
    (paper_balance, paper_initial_balance, onboarding_completed) are
    Optional with safe defaults — older rows have NULLs in those columns
    and we must not 500 the entire admin panel on a missing column.
    """
    id: str
    telegram_id: int
    telegram_username: Optional[str] = None
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: str = "user"
    status: str = "pending_approval"
    language: str = "en"
    trial_start: Optional[datetime] = None
    trial_end: Optional[datetime] = None
    approved_by_admin: bool = False
    default_account_type: str = "paper"
    paper_balance: Optional[float] = 10000.0
    paper_initial_balance: Optional[float] = 10000.0
    onboarding_completed: Optional[bool] = False
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_uuid_to_str(cls, v: Any) -> str:
        """Coerce uuid.UUID → str. Pydantic v2 strict mode refuses to
        auto-coerce, and SQLAlchemy returns a UUID object for UUID columns."""
        if v is None:
            return ""
        if isinstance(v, uuid.UUID):
            return str(v)
        return str(v)

    @field_validator("paper_balance", "paper_initial_balance", mode="before")
    @classmethod
    def _coerce_none_to_default(cls, v: Any) -> float:
        """Treat NULL database values as the $10,000 default instead of
        raising a ValidationError — older user rows predate these columns."""
        if v is None:
            return 10000.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 10000.0

    @field_validator("onboarding_completed", mode="before")
    @classmethod
    def _coerce_none_to_bool(cls, v: Any) -> bool:
        if v is None:
            return False
        return bool(v)

    @field_validator("approved_by_admin", mode="before")
    @classmethod
    def _coerce_none_approved(cls, v: Any) -> bool:
        if v is None:
            return False
        return bool(v)


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
