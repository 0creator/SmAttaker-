"""
SmAttaker — Audit Log Helper
Thin convenience wrapper around writing AdminAuditLog rows, so call
sites stay one line instead of repeating boilerplate everywhere.
"""
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from backend.models.admin_audit_log import AdminAuditLog
from backend.models.user import User


async def log_admin_action(
    db: AsyncSession,
    admin: User,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """
    Record an admin action. Deliberately does NOT commit — the caller's
    existing transaction (which is already committing the actual state
    change) covers this too, so the audit row and the change it
    describes are always atomically consistent with each other.
    """
    entry = AdminAuditLog(
        admin_user_id=admin.id,
        admin_telegram_id=admin.telegram_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details,
    )
    db.add(entry)
