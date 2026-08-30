"""add direction index + composite dedup indexes on signals

Revision ID: 0004_signal_indexes
Revises: 0003_paper_trading
Create Date: 2026-08-11

Supports the two dedup lookups strategies/runner.py runs per symbol on
EVERY strategy cycle (every STRATEGY_RUN_INTERVAL_MINUTES):

    WHERE symbol = ... AND direction = ... AND status = 'active'
    WHERE symbol = ... AND direction = ... AND created_at >= cutoff

Previously only `symbol` and `status` had single-column indexes, so
Postgres could use at most one of them per query and had to filter the
rest by scanning matching rows directly. As the `signals` table grows
(a new row roughly every 15 minutes, indefinitely, across every asset),
that filter step gets slower every month — a real, gradual contributor
to signals feeling "late," since a slow dedup check on one symbol
delays every symbol after it in the sequential analyze() loop.

Adds:
  - a single-column index on `direction` (was previously unindexed)
  - a composite index on (symbol, direction, created_at)
  - a composite index on (symbol, direction, status)

⚠️ Idempotent — checks existing indexes via inspector before creating,
so this is safe to run against a database that already has some of
these (e.g. a partial earlier deploy).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004_signal_indexes"
down_revision: Union[str, None] = "0003_paper_trading"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_index_names(table: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    existing = _existing_index_names("signals")

    if "ix_signals_direction" not in existing:
        op.create_index("ix_signals_direction", "signals", ["direction"])

    if "ix_signals_symbol_direction_created" not in existing:
        op.create_index(
            "ix_signals_symbol_direction_created",
            "signals",
            ["symbol", "direction", "created_at"],
        )

    if "ix_signals_symbol_direction_status" not in existing:
        op.create_index(
            "ix_signals_symbol_direction_status",
            "signals",
            ["symbol", "direction", "status"],
        )


def downgrade() -> None:
    existing = _existing_index_names("signals")

    if "ix_signals_symbol_direction_status" in existing:
        op.drop_index("ix_signals_symbol_direction_status", table_name="signals")
    if "ix_signals_symbol_direction_created" in existing:
        op.drop_index("ix_signals_symbol_direction_created", table_name="signals")
    if "ix_signals_direction" in existing:
        op.drop_index("ix_signals_direction", table_name="signals")
