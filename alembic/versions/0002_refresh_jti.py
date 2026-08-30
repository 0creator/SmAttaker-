"""add current_refresh_jti to users (refresh token rotation)

Revision ID: 0002_refresh_jti
Revises: 0001_baseline
Create Date: 2026-07-19

This is the first "real" migration since the baseline — a precise,
reviewable diff (add one nullable column), which is exactly the
autogenerate-style workflow the baseline's docstring said future
migrations should use.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_refresh_jti"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("current_refresh_jti", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "current_refresh_jti")
