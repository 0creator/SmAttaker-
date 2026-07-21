"""
SmAttaker — Payments & Webhooks API
Crypto payments via NOWPayments.
"""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db
from backend.config import settings
from backend.models.user import User, UserStatus
from backend.models.subscription import Subscription
from backend.models.admin_notification import AdminNotification, NotificationType
from backend.schemas.subscription import SubscriptionCreate, PaymentVerify, SubscriptionOut
from backend.schemas.common import APIResponse
from backend.api.auth import require_admin, get_current_user_dep
from backend.utils.security import validate_nowpayments_ipn
from backend.utils.rate_limit import rate_limiter
from backend.utils.audit import log_admin_action
from backend.models.admin_audit_log import AuditAction
import logging

logger = logging.getLogger("smattaker.payments")

router = APIRouter()
webhook_router = APIRouter()


# ── Wallet Info (for the web dashboard's subscribe flow) ─
@router.get("/wallet-info", response_model=APIResponse[dict])
async def get_wallet_info(
    user: User = Depends(get_current_user_dep),
):
    """
    Safe-to-display crypto addresses (per network, individually
    validated) + the subscription price, for the web dashboard's
    "Subscribe" flow. Reuses the exact same safety checks as the bot
    (backend/utils/wallets.py) — one source of truth, so a bad/
    wrong-network env var is caught the same way from either interface.
    """
    from backend.utils.wallets import get_safe_wallet_addresses, get_network_label
    wallets = get_safe_wallet_addresses()
    return APIResponse(data={
        "networks": [
            {"key": k, "label": get_network_label(k), "address": v}
            for k, v in wallets.items()
        ],
        "price_usd": settings.SUBSCRIPTION_PRICE_USD,
        "configured": bool(wallets),
    })


# ── Crypto: Create NOWPayments Invoice ──────────────────
@router.post("/crypto/invoice", response_model=APIResponse[dict])
async def create_crypto_invoice(
    currency: str = "USDT",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """Create a crypto payment invoice via NOWPayments for the logged-in user."""
    if not settings.NOWPAYMENTS_API_KEY:
        raise HTTPException(status_code=500, detail="Crypto payments are not configured.")

    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.NOWPAYMENTS_API_URL}/invoice",
                json={
                    "price_amount": settings.SUBSCRIPTION_PRICE_USD,
                    "price_currency": "usd",
                    "pay_currency": currency.lower(),
                    "order_id": f"sub_{user.id}_{int(datetime.now(timezone.utc).timestamp())}",
                    "order_description": f"SmAttaker Monthly Subscription — {user.telegram_username or user.telegram_id}",
                    "ipn_callback_url": f"{settings.WEBHOOK_URL}/nowpayments",
                    "success_url": f"{settings.RENDER_EXTERNAL_URL}/payment/success",
                    "cancel_url": f"{settings.RENDER_EXTERNAL_URL}/payment/cancel",
                },
                headers={
                    "x-api-key": settings.NOWPAYMENTS_API_KEY,
                    "Content-Type": "application/json",
                },
            )
            data = resp.json()
            if resp.status_code >= 400:
                raise HTTPException(status_code=400, detail=data.get("message", "Payment error"))

            return APIResponse(
                data={
                    "invoice_url": data.get("invoice_url"),
                    "invoice_id": data.get("id"),
                    "pay_address": data.get("pay_address"),
                    "pay_amount": data.get("pay_amount"),
                    "pay_currency": data.get("pay_currency"),
                },
                message="Crypto invoice created. Send exact amount to the address.",
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Payment service error: {str(e)}")


# ── Verify Crypto Payment (manual) ──────────────────────
@router.post(
    "/crypto/verify",
    response_model=APIResponse[dict],
    dependencies=[Depends(rate_limiter(max_requests=5, window_seconds=300, prefix="payment_verify"))],
)
async def verify_crypto_payment(
    payload: PaymentVerify,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_dep),
):
    """
    The logged-in user submits a TX hash for manual verification.
    Admin reviews and confirms via /crypto/confirm (admin-only).
    """
    now = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user.id,
        plan_type="monthly",
        amount_usd=settings.SUBSCRIPTION_PRICE_USD,
        payment_method="crypto",
        payment_status="pending",
        crypto_tx_hash=payload.tx_hash,
        crypto_currency=payload.currency,
        crypto_amount=payload.amount,
        start_date=now,
        end_date=now + timedelta(days=30),
    )
    db.add(sub)

    notif = AdminNotification(
        notification_type=NotificationType.NEW_PAYMENT,
        title="Crypto Payment Submitted",
        message=(
            f"User @{user.telegram_username or user.telegram_id} submitted a crypto payment.\n"
            f"TX: {payload.tx_hash}\nAmount: {payload.amount} {payload.currency}"
        ),
        severity="info",
        related_user_id=user.id,
        related_subscription_id=sub.id,
    )
    db.add(notif)

    return APIResponse(
        message="Payment submitted for verification. Admin will confirm shortly.",
    )


# ── Admin: List Pending Payments for Review ─────────────
@router.get("/pending", response_model=APIResponse[list[dict]])
async def list_pending_payments(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    All subscriptions awaiting manual review (crypto payments submitted
    via /crypto/verify, not yet confirmed or rejected). Powers the
    admin panel's Payments tab.
    """
    result = await db.execute(
        select(Subscription)
        .where(Subscription.payment_status == "pending")
        .order_by(Subscription.start_date.desc())
    )
    subs = result.scalars().all()

    user_ids = [s.user_id for s in subs]
    users_by_id = {}
    if user_ids:
        user_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_by_id = {u.id: u for u in user_result.scalars().all()}

    items = []
    for s in subs:
        u = users_by_id.get(s.user_id)
        items.append({
            "subscription_id": str(s.id),
            "user_id": str(s.user_id),
            "telegram_username": u.telegram_username if u else None,
            "telegram_id": u.telegram_id if u else None,
            "plan_type": s.plan_type,
            "amount_usd": s.amount_usd,
            "crypto_currency": s.crypto_currency,
            "crypto_tx_hash": s.crypto_tx_hash,
            "submitted_at": s.start_date.isoformat() if s.start_date else None,
        })

    return APIResponse(data=items)


# ── Admin: Confirm/Reject Crypto Payment ────────────────
@router.post("/crypto/confirm", response_model=APIResponse[dict])
async def admin_confirm_payment(
    subscription_id: str,
    approved: bool = True,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Admin manually confirms or rejects a crypto payment. Admin-only."""
    result = await db.execute(
        select(Subscription).where(Subscription.id == subscription_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")

    if approved:
        sub.payment_status = "paid"
        # Activate user
        user_result = await db.execute(
            select(User).where(User.id == sub.user_id)
        )
        user = user_result.scalar_one_or_none()
        if user:
            user.status = UserStatus.ACTIVE
            user.approved_by_admin = True

        notif = AdminNotification(
            notification_type=NotificationType.NEW_PAYMENT,
            title="Payment Confirmed",
            message=f"Admin confirmed crypto payment for subscription {subscription_id}.",
            severity="info",
            related_user_id=sub.user_id,
            related_subscription_id=sub.id,
        )
        db.add(notif)
        await log_admin_action(
            db, _admin, AuditAction.PAYMENT_CONFIRMED,
            target_type="subscription", target_id=str(sub.id),
            details={"target_user_id": str(sub.user_id), "amount_usd": sub.amount_usd},
        )

        return APIResponse(message="✅ Payment confirmed. User activated.")
    else:
        sub.payment_status = "cancelled"
        notif = AdminNotification(
            notification_type=NotificationType.PAYMENT_FAILED,
            title="Payment Rejected",
            message=f"Admin rejected crypto payment for subscription {subscription_id}.",
            severity="warning",
            related_user_id=sub.user_id,
            related_subscription_id=sub.id,
        )
        db.add(notif)
        await log_admin_action(
            db, _admin, AuditAction.PAYMENT_REJECTED,
            target_type="subscription", target_id=str(sub.id),
            details={"target_user_id": str(sub.user_id)},
        )
        return APIResponse(message="❌ Payment rejected.")


# ── NOWPayments IPN Webhook (auto-confirm) ──────────────
@webhook_router.post("/nowpayments")
async def nowpayments_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Handle NOWPayments IPN (Instant Payment Notification) — fully automatic.

    ⚠️ SECURITY: previously this endpoint trusted ANY JSON body with no
    signature check at all — anyone could POST a fake "payment finished"
    event and get a free subscription. NOWPayments signs every IPN with
    HMAC-SHA512 over the sorted JSON body in the `x-nowpayments-sig`
    header; we now verify it before doing anything.
    """
    raw_body = await request.body()
    sig = request.headers.get("x-nowpayments-sig", "")

    if not settings.NOWPAYMENTS_IPN_SECRET:
        logger.error("NOWPAYMENTS_IPN_SECRET not configured — rejecting IPN.")
        raise HTTPException(status_code=503, detail="Payment webhook not configured.")

    if not validate_nowpayments_ipn(raw_body, sig, settings.NOWPAYMENTS_IPN_SECRET):
        logger.warning("Rejected NOWPayments IPN with invalid/missing signature.")
        raise HTTPException(status_code=401, detail="Invalid signature.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    background_tasks.add_task(_process_nowpayments_ipn, data)
    return {"status": "received"}


async def _process_nowpayments_ipn(data: dict):
    """Process NOWPayments IPN asynchronously."""
    from backend.database import async_session_factory

    payment_status = data.get("payment_status")
    if payment_status not in ("finished", "confirmed"):
        return

    order_id = data.get("order_id", "")
    user_id = order_id.split("_")[1] if order_id.startswith("sub_") else None
    if not user_id:
        return

    async with async_session_factory() as db:
        try:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                return

            now = datetime.now(timezone.utc)
            sub = Subscription(
                user_id=user.id,
                plan_type="monthly",
                amount_usd=float(data.get("price_amount", settings.SUBSCRIPTION_PRICE_USD)),
                payment_method="crypto",
                payment_status="paid",
                crypto_tx_hash=data.get("payment_id"),
                crypto_currency=data.get("pay_currency"),
                crypto_amount=float(data.get("pay_amount", 0)),
                start_date=now,
                end_date=now + timedelta(days=30),
            )
            db.add(sub)

            user.status = UserStatus.ACTIVE
            user.approved_by_admin = True
            db.add(user)

            await db.commit()
            print(f"✅ Crypto payment auto-confirmed for user {user.telegram_id}")

        except Exception as e:
            await db.rollback()
            print(f"❌ NOWPayments webhook error: {e}")
