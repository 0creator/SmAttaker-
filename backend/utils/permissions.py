"""
SmAttaker — RBAC Permission System
====================================
Before this module, `require_admin` was binary: `user.role == "admin"`
gave a user total, unconditional access to every admin endpoint —
approving payments, banning users, changing global settings, and
(implicitly) becoming indistinguishable from the founder account. That
is fine for exactly one operator. It stops being fine the moment a
second person needs admin access — a support agent who should approve
trials but never touch payouts, an analyst who should see numbers but
never touch a user record.

This module adds a *tier* on top of the existing `role` column rather
than replacing it: `role` still answers "is this an admin at all?"
(used everywhere already, including the Telegram bot), and the new
`admin_role` column answers "which admin?" — see `AdminRole` below.

Design choice: fixed tiers with fixed permission sets, not a fully
dynamic per-user permission editor. A dynamic system is more flexible
on paper, but for a platform this size it mostly creates permission
sprawl that's impossible to audit ("why does this one user have this
one extra permission from six months ago?"). Four tiers, each with a
clear, memorable job description, is enough — and if a genuine need
for a fifth tier shows up, it's one dict entry away.
"""
from enum import Enum


class Permission(str, Enum):
    MANAGE_USERS = "manage_users"                # approve / ban / edit user accounts
    MANAGE_SUBSCRIPTIONS = "manage_subscriptions"  # grant / revoke subscriptions
    APPROVE_PAYMENTS = "approve_payments"          # confirm / reject manual payments
    MANAGE_SIGNALS = "manage_signals"              # create/edit/delete signals manually
    TRIGGER_STRATEGIES = "trigger_strategies"      # manually run the strategy engines
    MANAGE_SETTINGS = "manage_settings"            # edit AdminSetting key/value pairs
    VIEW_AUDIT_LOG = "view_audit_log"
    MANAGE_ADMINS = "manage_admins"                # promote/demote admins, change tiers
    BROADCAST_MESSAGES = "broadcast_messages"      # send platform-wide bot broadcasts
    VIEW_ANALYTICS = "view_analytics"
    VIEW_SYSTEM_HEALTH = "view_system_health"      # scheduler/engine diagnostics


class AdminRole(str, Enum):
    SUPER_ADMIN = "super_admin"   # everything, including managing other admins
    OPERATIONS = "operations"     # day-to-day running of the platform, no admin mgmt
    SUPPORT = "support"           # user-facing: approvals, payments, read-only elsewhere
    ANALYST = "analyst"           # read-only: audit log, analytics, system health


# The single source of truth for "what can this tier do". Change access
# by editing this table — never by special-casing an individual user
# id in code, which is exactly how permission systems become unauditable.
ROLE_PERMISSIONS: dict[AdminRole, set[Permission]] = {
    AdminRole.SUPER_ADMIN: set(Permission),  # every permission that exists
    AdminRole.OPERATIONS: {
        Permission.MANAGE_USERS,
        Permission.MANAGE_SUBSCRIPTIONS,
        Permission.APPROVE_PAYMENTS,
        Permission.MANAGE_SIGNALS,
        Permission.TRIGGER_STRATEGIES,
        Permission.MANAGE_SETTINGS,
        Permission.VIEW_AUDIT_LOG,
        Permission.BROADCAST_MESSAGES,
        Permission.VIEW_ANALYTICS,
        Permission.VIEW_SYSTEM_HEALTH,
        # deliberately NOT MANAGE_ADMINS — operations can run the
        # platform but cannot create new admins or change tiers.
    },
    AdminRole.SUPPORT: {
        Permission.MANAGE_USERS,
        Permission.APPROVE_PAYMENTS,
        Permission.VIEW_AUDIT_LOG,
        Permission.VIEW_ANALYTICS,
    },
    AdminRole.ANALYST: {
        Permission.VIEW_AUDIT_LOG,
        Permission.VIEW_ANALYTICS,
        Permission.VIEW_SYSTEM_HEALTH,
    },
}


def role_has_permission(admin_role: str | None, permission: Permission) -> bool:
    """
    True if the given admin_role value grants the given permission.

    A None/unset admin_role means "an admin created before this system
    existed" (or a fresh super-admin bootstrap) — treated as full
    SUPER_ADMIN access rather than silently locking someone out. Losing
    access with zero warning is a worse failure mode than temporarily
    over-granting a legacy account; the migration also backfills every
    existing admin to SUPER_ADMIN explicitly for exactly this reason,
    so this branch is a safety net, not the normal path.
    """
    if not admin_role:
        return True
    try:
        role_enum = AdminRole(admin_role)
    except ValueError:
        return False
    return permission in ROLE_PERMISSIONS.get(role_enum, set())


def permissions_for(admin_role: str | None) -> list[str]:
    """Full permission list for a given admin_role — used by the /me
    endpoint so the frontend can show/hide UI without guessing."""
    if not admin_role:
        return [p.value for p in Permission]
    try:
        role_enum = AdminRole(admin_role)
    except ValueError:
        return []
    return sorted(p.value for p in ROLE_PERMISSIONS.get(role_enum, set()))
