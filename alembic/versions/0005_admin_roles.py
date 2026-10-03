"""add admin_role column for RBAC tiers

Revision ID: 0005_admin_roles
Revises: 0004_signal_indexes
Create Date: 2026-08-18

Adds one column to `users`:

  - admin_role   VARCHAR(32) NULL

Only meaningful when `role = 'admin'`. NULL means "no tier assigned" —
`role_has_permission()` in backend/utils/permissions.py treats NULL as
full SUPER_ADMIN access (see that module's docstring for why), so this
migration also backfills every *existing* admin to the explicit
'super_admin' value below. That backfill is what actually matters:
it makes today's admins' access an explicit, auditable fact in the
database instead of an implicit fallback, before any second admin
with a lesser tier is ever created.

⚠️ Idempotent, matching every other migration in this project — safe
to run multiple times across Render deploys.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0005_admin_roles"
down_revision: Union[str, None] = "0004_signal_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column("users", "admin_role"):
        op.add_column(
            "users",
            sa.Column("admin_role", sa.String(length=32), nullable=True),
        )
    # Backfill: every account that is currently an admin becomes an
    # explicit 'super_admin' — preserving exactly the unconditional
    # access they already had, just made explicit instead of implicit.
    op.execute(
        "UPDATE users SET admin_role = 'super_admin' "
        "WHERE role = 'admin' AND admin_role IS NULL"
    )


def downgrade() -> None:
    if _has_column("users", "admin_role"):
        op.drop_column("users", "admin_role")
