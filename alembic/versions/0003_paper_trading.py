"""add paper trading + onboarding columns to users

Revision ID: 0003_paper_trading
Revises: 0002_refresh_jti
Create Date: 2026-08-09

Adds three columns to the `users` table to support the new
trading-type onboarding flow:

  - paper_balance            FLOAT NOT NULL DEFAULT 10000.0
  - paper_initial_balance    FLOAT NOT NULL DEFAULT 10000.0
  - onboarding_completed     BOOLEAN NOT NULL DEFAULT FALSE

Also changes the default of `default_account_type` from 'demo' to
'paper' (the new default for users who haven't chosen yet).

⚠️ This migration is idempotent — it checks `inspector.get_columns()`
before adding each column, so running it on a database that already
has some of these columns (e.g. from a partial earlier run) won't
fail. This is critical for Render deploys where the migration may
run multiple times across deploys.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_paper_trading"
down_revision: Union[str, None] = "0002_refresh_jti"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    """Check if a column already exists (idempotent migration)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    # Add new columns (idempotent)
    if not _has_column("users", "paper_balance"):
        op.add_column(
            "users",
            sa.Column("paper_balance", sa.Float(), nullable=False, server_default="10000.0"),
        )
    if not _has_column("users", "paper_initial_balance"):
        op.add_column(
            "users",
            sa.Column("paper_initial_balance", sa.Float(), nullable=False, server_default="10000.0"),
        )
    if not _has_column("users", "onboarding_completed"):
        op.add_column(
            "users",
            sa.Column("onboarding_completed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )

    # Change default_account_type default from 'demo' to 'paper'
    # (existing rows keep their value; only NEW rows get 'paper')
    op.alter_column(
        "users",
        "default_account_type",
        server_default="paper",
    )


def downgrade() -> None:
    # Revert default
    op.alter_column(
        "users",
        "default_account_type",
        server_default="demo",
    )
    # Drop columns (idempotent)
    if _has_column("users", "onboarding_completed"):
        op.drop_column("users", "onboarding_completed")
    if _has_column("users", "paper_initial_balance"):
        op.drop_column("users", "paper_initial_balance")
    if _has_column("users", "paper_balance"):
        op.drop_column("users", "paper_balance")
