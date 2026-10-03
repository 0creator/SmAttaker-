"""
SmAttaker — Admin Management API (RBAC)
=========================================
Endpoints for managing *admins themselves* — who has admin access, at
what tier, and the global platform settings only admins should touch.
Separate from users.py (which manages regular platform users) because
these routes are inherently more sensitive: a bug here can create or
destroy admin access, not just ban/approve a trader.

Every mutating endpoint is permission-gated via `require_permission`
(see backend/utils/permissions.py) and writes an AdminAuditLog row —
consistent with the rest of the platform's existing audit trail.

Response bodies are plain dicts rather than strict Pydantic schemas,
matching the pattern already used in users.py's audit-log endpoint —
that file's own docstring explains why: a strict schema 500'd the
entire admin panel in the past when a field was unexpectedly NULL on
older rows. Plain dicts with `.get()`-style safe defaults avoid that
failure mode entirely for what is, after all, internal admin tooling.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional
from pydantic import BaseModel

from backend.database import get_db
from backend.models.user import User, UserRole
from backend.models.admin_settings import AdminSetting
from backend.models.admin_audit_log import AuditAction
from backend.schemas.common import APIResponse, PaginatedResponse
from backend.api.auth import require_permission, require_admin, get_current_user_dep
from backend.utils.audit import log_admin_action
from backend.utils.permissions import Permission, AdminRole, permissions_for

router = APIRouter()


# ── Who am I / what can I do ──────────────────────────────
@router.get("/me/permissions", response_model=APIResponse[dict])
async def my_permissions(admin: User = Depends(require_admin)):
    """
    What the currently-authenticated admin can do. The admin web panel
    calls this once on load to decide which tabs/buttons to show —
    the backend is still the real enforcement point (every mutating
    endpoint checks again), this is purely so the UI doesn't dangle
    controls in front of someone who'll just get a 403.
    """
    admin_role = getattr(admin, "admin_role", None)
    return APIResponse(data={
        "admin_role": admin_role or AdminRole.SUPER_ADMIN.value,
        "permissions": permissions_for(admin_role),
        "available_roles": [r.value for r in AdminRole],
    })


# ── Admin roster ───────────────────────────────────────────
@router.get("/admins", response_model=APIResponse[list[dict]])
async def list_admins(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_permission(Permission.MANAGE_ADMINS)),
):
    """Every account with role=admin, and their assigned tier."""
    result = await db.execute(select(User).where(User.role == UserRole.ADMIN))
    admins = result.scalars().all()
    return APIResponse(data=[
        {
            "id": str(a.id),
            "telegram_id": a.telegram_id,
            "telegram_username": a.telegram_username,
            "full_name": a.full_name,
            "admin_role": a.admin_role or AdminRole.SUPER_ADMIN.value,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in admins
    ])


class PromoteRequest(BaseModel):
    telegram_id: int
    admin_role: str


@router.post("/admins/promote", response_model=APIResponse[dict])
async def promote_to_admin(
    body: PromoteRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.MANAGE_ADMINS)),
):
    """Grant admin access (at the given tier) to an existing platform
    user, identified by their Telegram ID. The user must already have
    signed up through the bot/dashboard — this does not create new
    accounts, only elevates an existing one."""
    try:
        role_enum = AdminRole(body.admin_role)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown admin_role. Must be one of: {[r.value for r in AdminRole]}",
        )

    result = await db.execute(select(User).where(User.telegram_id == body.telegram_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="No user found with that Telegram ID.")

    target.role = UserRole.ADMIN
    target.admin_role = role_enum.value

    await log_admin_action(
        db, admin, AuditAction.ADMIN_ROLE_ASSIGNED,
        target_type="user", target_id=str(target.id),
        details={"telegram_id": target.telegram_id, "admin_role": role_enum.value},
    )
    await db.commit()
    return APIResponse(data={
        "id": str(target.id), "telegram_id": target.telegram_id, "admin_role": role_enum.value,
    }, message=f"{target.telegram_username or target.telegram_id} is now a {role_enum.value} admin.")


class ChangeTierRequest(BaseModel):
    admin_role: str


async def _count_super_admins(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(User).where(
            User.role == UserRole.ADMIN,
            (User.admin_role == AdminRole.SUPER_ADMIN.value) | (User.admin_role.is_(None)),
        )
    )
    return result.scalar_one()


@router.put("/admins/{user_id}/role", response_model=APIResponse[dict])
async def change_admin_tier(
    user_id: str,
    body: ChangeTierRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.MANAGE_ADMINS)),
):
    """Change an existing admin's tier."""
    try:
        role_enum = AdminRole(body.admin_role)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown admin_role. Must be one of: {[r.value for r in AdminRole]}",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target or target.role != UserRole.ADMIN:
        raise HTTPException(status_code=404, detail="No admin found with that id.")

    was_super = (target.admin_role or AdminRole.SUPER_ADMIN.value) == AdminRole.SUPER_ADMIN.value
    if was_super and role_enum != AdminRole.SUPER_ADMIN:
        # Lockout guard: never allow the last super-admin to be downgraded.
        if await _count_super_admins(db) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Refusing to downgrade the last super_admin — this would lock everyone "
                       "out of admin management. Promote another super_admin first.",
            )

    target.admin_role = role_enum.value
    await log_admin_action(
        db, admin, AuditAction.ADMIN_ROLE_ASSIGNED,
        target_type="user", target_id=str(target.id),
        details={"telegram_id": target.telegram_id, "admin_role": role_enum.value},
    )
    await db.commit()
    return APIResponse(data={"id": str(target.id), "admin_role": role_enum.value})


@router.post("/admins/{user_id}/demote", response_model=APIResponse[dict])
async def demote_admin(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.MANAGE_ADMINS)),
):
    """Remove admin access entirely — the account reverts to a normal
    platform user. Cannot be used to remove the last super_admin."""
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target or target.role != UserRole.ADMIN:
        raise HTTPException(status_code=404, detail="No admin found with that id.")

    was_super = (target.admin_role or AdminRole.SUPER_ADMIN.value) == AdminRole.SUPER_ADMIN.value
    if was_super and await _count_super_admins(db) <= 1:
        raise HTTPException(
            status_code=409,
            detail="Refusing to demote the last super_admin — this would lock everyone "
                   "out of admin management. Promote another super_admin first.",
        )

    target.role = UserRole.USER
    target.admin_role = None
    await log_admin_action(
        db, admin, AuditAction.ADMIN_DEMOTED,
        target_type="user", target_id=str(target.id),
        details={"telegram_id": target.telegram_id},
    )
    await db.commit()
    return APIResponse(message=f"{target.telegram_username or target.telegram_id} is no longer an admin.")


# ── Global platform settings ───────────────────────────────
@router.get("/settings", response_model=APIResponse[list[dict]])
async def list_settings(
    category: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_permission(Permission.MANAGE_SETTINGS)),
):
    query = select(AdminSetting).order_by(AdminSetting.category, AdminSetting.setting_key)
    if category:
        query = query.where(AdminSetting.category == category)
    result = await db.execute(query)
    settings_rows = result.scalars().all()
    return APIResponse(data=[
        {
            "key": s.setting_key,
            "value": s.setting_value,
            "description": s.description,
            "category": s.category,
        }
        for s in settings_rows
    ])


class SettingUpsert(BaseModel):
    value: str
    description: Optional[str] = None
    category: Optional[str] = "general"


@router.put("/settings/{key}", response_model=APIResponse[dict])
async def upsert_setting(
    key: str,
    body: SettingUpsert,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.MANAGE_SETTINGS)),
):
    """Create or update a global setting. Every change is audit-logged
    with the old and new value so a bad setting change is always
    traceable to exactly who made it and when."""
    result = await db.execute(select(AdminSetting).where(AdminSetting.setting_key == key))
    setting = result.scalar_one_or_none()
    old_value = setting.setting_value if setting else None

    if setting:
        setting.setting_value = body.value
        if body.description is not None:
            setting.description = body.description
        if body.category is not None:
            setting.category = body.category
    else:
        setting = AdminSetting(
            setting_key=key, setting_value=body.value,
            description=body.description, category=body.category or "general",
        )
        db.add(setting)

    await log_admin_action(
        db, admin, AuditAction.ADMIN_SETTING_CHANGED,
        target_type="admin_setting", target_id=key,
        details={"old_value": old_value, "new_value": body.value},
    )
    await db.commit()
    return APIResponse(data={"key": key, "value": body.value}, message=f"Setting '{key}' saved.")
