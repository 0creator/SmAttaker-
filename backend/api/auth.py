"""
SmAttaker — Auth API Routes
Login, Registration, Trial Requests
"""
from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from backend.database import get_db
from backend.config import settings
from backend.models.user import User, UserRole, UserStatus
from backend.models.admin_notification import AdminNotification, NotificationType
from backend.schemas.user import (
    UserCreate, UserOut, UserLoginRequest,
    TrialRequest, TrialApproval,
)
from backend.schemas.common import APIResponse
from backend.utils.security import (
    create_access_token, create_refresh_token, decode_token, validate_telegram_hash,
    verify_internal_api_key,
)
from backend.utils.rate_limit import rate_limiter
from backend.utils.audit import log_admin_action
from backend.models.admin_audit_log import AuditAction

router = APIRouter()
bearer_scheme_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


# ── Real Auth Dependency (reads + verifies JWT) ─────────
# Defined near the top of the module (not at the bottom) because several
# routes below reference `require_admin` in their own default-argument
# list, which Python resolves at import time — referencing a name that's
# defined later in the file raises NameError as soon as the module loads.
async def get_current_user_dep(
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(default=None),
) -> User:
    """
    Extract and verify the JWT from the `Authorization: Bearer <token>`
    header, then load the corresponding user from the database.
    Replaces the previous placeholder that returned the first DB row
    for ANY request regardless of whether a token was even sent.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise bearer_scheme_error
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise bearer_scheme_error

    user_id = payload.get("sub")
    if not user_id:
        raise bearer_scheme_error

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise bearer_scheme_error
    if user.is_banned:
        raise HTTPException(status_code=403, detail="Your account has been banned.")
    return user


async def require_admin(
    user: User = Depends(get_current_user_dep),
) -> User:
    """Dependency for admin-only endpoints. Use on every sensitive route."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    return user


# ── Telegram Login / Register ───────────────────────────
@router.post(
    "/login",
    response_model=APIResponse[dict],
    dependencies=[Depends(rate_limiter(max_requests=10, window_seconds=60, prefix="login"))],
)
async def login_or_register(
    req: UserLoginRequest,
    db: AsyncSession = Depends(get_db),
    x_internal_api_key: Optional[str] = Header(default=None),
):
    """
    Telegram-based authentication.

    This endpoint issues a JWT for a given telegram_id, so it MUST prove
    the caller actually controls that Telegram identity. Two accepted
    proofs (either one is required — the endpoint no longer trusts a
    bare telegram_id with nothing behind it):

      1. A valid Telegram Login Widget payload: `hash` + `auth_date`
         (and the other widget fields) verified via HMAC against the
         bot token (see utils.security.validate_telegram_hash).
      2. A trusted internal caller (our own Telegram bot backend, which
         already authenticated the user via Telegram's own bot API)
         presenting X-Internal-Api-Key.

    If user exists → login (return JWT).
    If user is new → register (pending admin approval) and return status.
    """
    telegram_payload = req.model_dump(exclude_none=True)
    has_widget_proof = "hash" in telegram_payload and "auth_date" in telegram_payload
    is_internal = verify_internal_api_key(x_internal_api_key)

    if not is_internal:
        if not has_widget_proof:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Telegram identity not verified. Provide a valid Telegram "
                    "Login Widget payload (hash + auth_date) or call this "
                    "endpoint from a trusted internal service."
                ),
            )
        if not settings.TELEGRAM_BOT_TOKEN or not validate_telegram_hash(
            telegram_payload, settings.TELEGRAM_BOT_TOKEN
        ):
            raise HTTPException(status_code=401, detail="Invalid Telegram login signature.")

    # Look up by telegram_id
    result = await db.execute(
        select(User).where(User.telegram_id == req.telegram_id)
    )
    user = result.scalar_one_or_none()

    if user:
        # Existing user
        if user.is_banned:
            raise HTTPException(status_code=403, detail="Your account has been banned.")

        # Update username if changed
        if req.username and req.username != user.telegram_username:
            user.telegram_username = req.username
        if req.language_code and req.language_code != user.language:
            user.language = req.language_code

        token = create_access_token(str(user.id), user.telegram_id)
        refresh_token, refresh_jti = create_refresh_token(str(user.id), user.telegram_id)
        user.current_refresh_jti = refresh_jti
        return APIResponse(
            data={
                "token": token,
                "refresh_token": refresh_token,
                "user": UserOut.model_validate(user).model_dump(),
                "is_new": False,
            },
            message="Welcome back! 🦅",
        )

    # New user — register with pending_approval
    new_user = User(
        telegram_id=req.telegram_id,
        telegram_username=req.username,
        full_name=f"{req.first_name or ''} {req.last_name or ''}".strip() or None,
        language=req.language_code or "en",
        role=UserRole.USER,
        status=UserStatus.PENDING_APPROVAL,
    )
    db.add(new_user)
    await db.flush()

    # Notify admin
    notif = AdminNotification(
        notification_type=NotificationType.NEW_REGISTRATION,
        title="New User Registered",
        message=f"@{req.username or req.telegram_id} has joined SmAttaker and is pending approval.",
        severity="info",
        related_user_id=new_user.id,
    )
    db.add(notif)

    token = create_access_token(str(new_user.id), new_user.telegram_id)
    refresh_token, refresh_jti = create_refresh_token(str(new_user.id), new_user.telegram_id)
    new_user.current_refresh_jti = refresh_jti
    return APIResponse(
        data={
            "token": token,
            "refresh_token": refresh_token,
            "user": UserOut.model_validate(new_user).model_dump(),
            "is_new": True,
        },
        message="Account created! Awaiting admin approval. 🦅",
    )


# ── Request Free Trial ──────────────────────────────────
@router.post("/trial/request", response_model=APIResponse[dict])
async def request_trial(
    req: TrialRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    User requests the 3-day free trial.
    Requires admin approval before activation.
    """
    result = await db.execute(
        select(User).where(User.telegram_id == req.telegram_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Please /start first.")
    if user.status == UserStatus.TRIAL:
        raise HTTPException(status_code=400, detail="You already have an active trial.")
    if user.status == UserStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="You already have an active subscription.")

    # Update email
    user.email = req.email
    user.status = UserStatus.PENDING_APPROVAL

    # Notify admin
    notif = AdminNotification(
        notification_type=NotificationType.TRIAL_REQUEST,
        title="Trial Request",
        message=(
            f"@{user.telegram_username or user.telegram_id} ({req.email}) "
            f"is requesting a 3-day free trial."
        ),
        severity="info",
        related_user_id=user.id,
    )
    db.add(notif)

    return APIResponse(
        data={"user_id": str(user.id), "email": req.email},
        message="Trial request submitted! Admin will review and approve. 📩",
    )


# ── Admin: Approve / Reject Trial ───────────────────────
@router.post("/admin/trial/approve", response_model=APIResponse[dict])
async def approve_trial(
    req: TrialApproval,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Admin approves or rejects a trial request. Admin-only."""
    result = await db.execute(
        select(User).where(User.id == req.user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    from datetime import datetime, timedelta, timezone
    from backend.config import settings

    if req.approved:
        now = datetime.now(timezone.utc)
        user.status = UserStatus.TRIAL
        user.approved_by_admin = True
        user.trial_start = now
        user.trial_end = now + timedelta(days=settings.TRIAL_DAYS)
        db.add(user)

        notif = AdminNotification(
            notification_type=NotificationType.TRIAL_REQUEST,
            title="Trial Approved",
            message=f"Trial approved for @{user.telegram_username or user.telegram_id}.",
            severity="info",
            related_user_id=user.id,
        )
        db.add(notif)
        await log_admin_action(
            db, _admin, AuditAction.TRIAL_APPROVED,
            target_type="user", target_id=str(user.id),
            details={"target_telegram_id": user.telegram_id, "trial_days": settings.TRIAL_DAYS},
        )

        return APIResponse(
            message=f"✅ Trial approved for {user.telegram_username or user.telegram_id}. Expires in {settings.TRIAL_DAYS} days.",
        )
    else:
        user.status = UserStatus.INACTIVE
        db.add(user)

        notif = AdminNotification(
            notification_type=NotificationType.TRIAL_REQUEST,
            title="Trial Rejected",
            message=f"Trial rejected for @{user.telegram_username or user.telegram_id}. Reason: {req.reason or 'N/A'}",
            severity="info",
            related_user_id=user.id,
        )
        db.add(notif)
        await log_admin_action(
            db, _admin, AuditAction.TRIAL_REJECTED,
            target_type="user", target_id=str(user.id),
            details={"target_telegram_id": user.telegram_id, "reason": req.reason},
        )

        return APIResponse(
            message=f"❌ Trial rejected. Reason: {req.reason or 'N/A'}",
        )


# ── Refresh Access Token (with rotation) ────────────────
@router.post(
    "/refresh",
    response_model=APIResponse[dict],
    dependencies=[Depends(rate_limiter(max_requests=20, window_seconds=60, prefix="refresh"))],
)
async def refresh_access_token(
    refresh_token: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange a refresh token for a new access token + a NEW refresh
    token (rotation — the old refresh token is invalidated the moment
    this succeeds, so it can never be used a second time).

    ⚠️ SECURITY: if a refresh token is presented whose `jti` doesn't
    match the user's `current_refresh_jti`, that token has already been
    rotated out — either it's stale (client held onto an old one) or it
    was stolen and someone else already used it. Either way we don't
    trust it: the user's stored jti is cleared, forcing a fresh login,
    rather than silently accepting a token that shouldn't still work.
    """
    payload = decode_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    user_id = payload.get("sub")
    presented_jti = payload.get("jti")
    if not user_id or not presented_jti:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    if user.current_refresh_jti != presented_jti:
        # Reuse of a rotated-out token, or a stale/forged one — treat as
        # a security event: kill the stored jti so even a legitimate but
        # confused client can't keep retrying with an old token, and
        # make them log in again properly.
        user.current_refresh_jti = None
        raise HTTPException(
            status_code=401,
            detail="Refresh token already used or invalid. Please log in again.",
        )

    if user.is_banned:
        raise HTTPException(status_code=403, detail="Your account has been banned.")

    new_access_token = create_access_token(str(user.id), user.telegram_id)
    new_refresh_token, new_jti = create_refresh_token(str(user.id), user.telegram_id)
    user.current_refresh_jti = new_jti

    return APIResponse(data={"token": new_access_token, "refresh_token": new_refresh_token})


# ── Get Current User Profile ────────────────────────────
@router.get("/me", response_model=APIResponse[UserOut])
async def get_current_user(
    user: User = Depends(get_current_user_dep),
):
    """Get the currently authenticated user's profile."""
    return APIResponse(data=UserOut.model_validate(user))
