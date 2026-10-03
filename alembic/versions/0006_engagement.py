"""add user_engagement table (discipline streaks + badges)

Revision ID: 0006_engagement
Revises: 0005_admin_roles
Create Date: 2026-08-19

Adds a new `user_engagement` table — one row per user, created lazily
by the engagement service on first evaluation (not backfilled here,
since a user with zero trades has nothing to measure yet).

See backend/models/user_engagement.py and backend/services/engagement.py
for what this powers: a risk-discipline streak (consecutive days
without exceeding the user's own declared max-risk-per-trade limit),
a small set of process-based badges, and opt-in periodic digest
preferences.

⚠️ Idempotent, matching every other migration in this project.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_engagement"
down_revision: Union[str, None] = "0005_admin_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("user_engagement"):
        return

    op.create_table(
        "user_engagement",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True,
        ),
        sa.Column("discipline_streak_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discipline_streak_best", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_evaluated_date", sa.Date(), nullable=True),
        sa.Column("last_violation_date", sa.Date(), nullable=True),
        sa.Column("badges", postgresql.JSONB(), nullable=True),
        sa.Column("digest_frequency", sa.String(length=16), nullable=False, server_default="weekly"),
        sa.Column("last_digest_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_user_engagement_user_id", "user_engagement", ["user_id"])


def downgrade() -> None:
    if _has_table("user_engagement"):
        op.drop_index("ix_user_engagement_user_id", table_name="user_engagement")
        op.drop_table("user_engagement")
