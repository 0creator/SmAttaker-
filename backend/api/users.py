"""
SmAttaker — Users API Routes
Admin user management.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from backend.database import get_db
from backend.models.user import User, UserStatus, UserRole
from backend.models.admin_audit_log import AuditAction
from backend.schemas.user import UserOut, UserAdminOut
from backend.schemas.common import APIResponse, PaginatedResponse
from backend.api.auth import require_admin
from backend.utils.rate_limit import rate_limiter
from backend.utils.audit import log_admin_action

router = APIRouter()


@router.get("/", response_model=APIResponse[PaginatedResponse[UserAdminOut]])
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """List all users (admin only). Supports filtering and search."""
    query = select(User)

    if status:
        query = query.where(User.status == status)
    if search:
        query = query.where(
            (User.telegram_username.ilike(f"%{search}%"))
            | (User.email.ilike(f"%{search}%"))
            | (User.full_name.ilike(f"%{search}%"))
        )

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Paginate
    query = query.order_by(User.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    users = result.scalars().all()

    items = [
        UserAdminOut(
            **UserOut.model_validate(u).model_dump(),
            total_trades=len(u.trades) if u.trades else 0,
            active_subscription=any(s.is_active for s in u.subscriptions) if u.subscriptions else False,
        )
        for u in users
    ]

    return APIResponse(
        data=PaginatedResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size,
        )
    )


@router.put("/{user_id}/status", response_model=APIResponse[UserOut])
async def update_user_status(
    user_id: str,
    status: str,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Admin: update user status (active, banned, inactive). Admin-only."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    valid_statuses = [UserStatus.ACTIVE, UserStatus.BANNED, UserStatus.INACTIVE]
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use: {valid_statuses}")

    old_status = user.status
    user.status = status
    db.add(user)
    await log_admin_action(
        db, _admin, AuditAction.USER_STATUS_CHANGED,
        target_type="user", target_id=str(user.id),
        details={"old_status": old_status, "new_status": status, "target_telegram_id": user.telegram_id},
    )
    return APIResponse(data=UserOut.model_validate(user), message=f"User status updated to {status}.")


# ── Audit Log ────────────────────────────────────────────
@router.get("/audit-log", response_model=APIResponse[PaginatedResponse[dict]])
async def get_audit_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    Full history of sensitive admin actions — who did what, to whom,
    when. Admin-only. This is the accountability trail an institutional
    platform needs; every status change, payment decision, and trial
    approval in the system is recorded here permanently.
    """
    from backend.models.admin_audit_log import AdminAuditLog

    query = select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc())
    if action:
        query = query.where(AdminAuditLog.action == action)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    entries = result.scalars().all()

    items = [
        {
            "id": str(e.id),
            "admin_telegram_id": e.admin_telegram_id,
            "action": e.action,
            "target_type": e.target_type,
            "target_id": e.target_id,
            "details": e.details,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]

    return APIResponse(data=PaginatedResponse(
        items=items, total=total, page=page, page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    ))


# ── Broadcast Message ────────────────────────────────────
@router.post(
    "/broadcast",
    response_model=APIResponse[dict],
    dependencies=[Depends(rate_limiter(max_requests=3, window_seconds=3600, prefix="broadcast"))],
)
async def broadcast_message(
    message: str = Query(..., min_length=1, max_length=2000),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    Send a message to every active/trial user via Telegram — the web
    panel's equivalent of the bot's /admin_broadcast command (same
    underlying action, available from either interface). Heavily rate
    limited since a mistaken double-click here messages every user.
    """
    from telegram import Bot
    from backend.config import settings

    if not settings.TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Bot token not configured.")

    result = await db.execute(
        select(User).where(User.status.in_([UserStatus.ACTIVE, UserStatus.TRIAL]))
    )
    recipients = result.scalars().all()

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    sent, failed = 0, 0
    for recipient in recipients:
        try:
            await bot.send_message(
                chat_id=recipient.telegram_id,
                text=f"📢 *Announcement*\n\n{message}",
                parse_mode="Markdown",
            )
            sent += 1
        except Exception:
            failed += 1

    await log_admin_action(
        db, _admin, AuditAction.BROADCAST_SENT,
        details={"recipients": len(recipients), "sent": sent, "failed": failed, "message_preview": message[:200]},
    )

    return APIResponse(data={"sent": sent, "failed": failed, "total_recipients": len(recipients)})


# ── Admin: Grant/Extend Subscription Manually ───────────
@router.post("/{user_id}/grant-subscription", response_model=APIResponse[dict])
async def grant_subscription(
    user_id: str,
    plan_type: str = Query("monthly", pattern="^(trial|monthly|lifetime)$"),
    days: int = Query(30, ge=1, le=3650),
    reason: Optional[str] = Query(None, max_length=500),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    Full admin control: activate or extend a user's subscription
    directly, bypassing the payment flow entirely — for comps, VIP
    grants, support resolutions, or manual approvals outside crypto
    payment. Also flips the user's own status to active if they were
    pending/inactive, since a granted subscription with no active
    account would be a confusing half-state.
    """
    from datetime import datetime, timedelta, timezone
    from backend.models.subscription import Subscription

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    now = datetime.now(timezone.utc)
    end_date = None if plan_type == "lifetime" else now + timedelta(days=days)

    sub = Subscription(
        user_id=user.id,
        plan_type=plan_type,
        amount_usd=0.0,
        payment_method="admin_grant",
        payment_status="paid",
        start_date=now,
        end_date=end_date,
        auto_renew=False,
    )
    db.add(sub)

    if user.status != UserStatus.ACTIVE:
        user.status = UserStatus.ACTIVE
        db.add(user)

    await log_admin_action(
        db, _admin, AuditAction.SUBSCRIPTION_GRANTED,
        target_type="user", target_id=str(user.id),
        details={
            "target_telegram_id": user.telegram_id, "plan_type": plan_type,
            "days": days if plan_type != "lifetime" else None, "reason": reason,
        },
    )

    return APIResponse(
        data={"subscription_id": str(sub.id), "plan_type": plan_type, "end_date": end_date.isoformat() if end_date else None},
        message=f"Subscription granted to @{user.telegram_username or user.telegram_id}.",
    )


# ── Admin: Full User Detail (drill-down) ────────────────
@router.get("/{user_id}/detail", response_model=APIResponse[dict])
async def get_user_detail(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    Everything about one user in a single call: profile, subscriptions,
    trades, exchange connections, risk settings — powers the admin
    panel's user drill-down modal so support/investigation doesn't
    require cross-referencing four separate tables by hand.
    """
    from backend.models.trade import Trade, TradeStatus
    from backend.models.subscription import Subscription
    from backend.models.exchange_connection import ExchangeConnection
    from backend.models.risk_settings import RiskSettings

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    trades_result = await db.execute(
        select(Trade).where(Trade.user_id == user.id).order_by(Trade.created_at.desc()).limit(50)
    )
    trades = trades_result.scalars().all()

    subs_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.start_date.desc())
    )
    subs = subs_result.scalars().all()

    exch_result = await db.execute(select(ExchangeConnection).where(ExchangeConnection.user_id == user.id))
    exchanges = exch_result.scalars().all()

    risk_result = await db.execute(select(RiskSettings).where(RiskSettings.user_id == user.id))
    risk = risk_result.scalars().all()

    completed = [t for t in trades if t.status == TradeStatus.COMPLETED]
    wins = [t for t in completed if t.is_winner]

    return APIResponse(data={
        "user": {
            "id": str(user.id), "telegram_id": user.telegram_id,
            "telegram_username": user.telegram_username, "full_name": user.full_name,
            "email": user.email, "role": user.role, "status": user.status,
            "language": user.language, "created_at": user.created_at.isoformat(),
            "trial_start": user.trial_start.isoformat() if user.trial_start else None,
            "trial_end": user.trial_end.isoformat() if user.trial_end else None,
        },
        "stats": {
            "total_trades": len(trades),
            "completed_trades": len(completed),
            "win_rate": round(len(wins) / len(completed) * 100, 1) if completed else 0,
            "total_pnl_usd": round(sum(t.pnl or 0 for t in completed), 2),
        },
        "recent_trades": [
            {
                "id": str(t.id), "symbol": t.symbol, "direction": t.direction,
                "status": t.status, "pnl_percent": t.pnl_percent,
                "created_at": t.created_at.isoformat(),
            } for t in trades[:20]
        ],
        "subscriptions": [
            {
                "id": str(s.id), "plan_type": s.plan_type, "payment_status": s.payment_status,
                "amount_usd": s.amount_usd, "start_date": s.start_date.isoformat() if s.start_date else None,
                "end_date": s.end_date.isoformat() if s.end_date else None,
            } for s in subs
        ],
        "exchange_connections": [
            {
                "id": str(e.id), "exchange_name": e.exchange_name, "is_active": e.is_active,
                "connection_status": e.connection_status,
            } for e in exchanges
        ],
        "risk_settings": [
            {
                "account_type": r.account_type, "max_risk_per_trade_pct": r.max_risk_per_trade_pct,
                "max_leverage": r.max_leverage, "position_sizing_method": r.position_sizing_method,
            } for r in risk
        ],
    })
