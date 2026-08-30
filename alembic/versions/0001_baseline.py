"""baseline — adopt Alembic on top of the existing create_all()-managed schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-18

⚠️ WHY THIS MIGRATION LOOKS LIKE THIS (read before adding the next one):

This project's tables were originally created via
`Base.metadata.create_all()` at app startup (see backend/database.py),
not via migrations — there was no Alembic history until now. Two
audiences need this same migration to do different things:

  1. A BRAND NEW database (fresh install): `alembic upgrade head` needs
     to actually create every table from scratch.
  2. The EXISTING production database (already has all these tables,
     created by create_all()): running `create_all()` again is a safe
     no-op (create_all only creates tables that don't already exist),
     so `alembic upgrade head` is ALSO safe to run directly here —
     no separate `stamp head` step needed.

Rather than hand-transcribing every column, type, index, and foreign
key for 9 tables into `op.create_table(...)` calls by hand (a real risk
of transcription mistakes with no live Postgres available to test
against), this baseline reuses `Base.metadata` directly — the exact
same source of truth the running app already trusts. This is a
well-established, documented pattern for adopting Alembic mid-project.

From the NEXT migration onward, use real `alembic revision --autogenerate`
against a real database so changes are precise, reviewable diffs instead
of full-metadata dumps like this one.
"""
from typing import Sequence, Union

from alembic import op

# Import the app's Base + all model modules so metadata is fully
# populated before create_all/drop_all run — same imports as env.py.
from backend.database import Base
import backend.models.signal              # noqa: F401
import backend.models.user                # noqa: F401
import backend.models.trade               # noqa: F401
import backend.models.subscription        # noqa: F401
import backend.models.risk_settings       # noqa: F401
import backend.models.exchange_connection  # noqa: F401
import backend.models.admin_settings      # noqa: F401
import backend.models.admin_notification  # noqa: F401
import backend.models.admin_audit_log     # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    # Deliberately a no-op rather than dropping every table — a
    # downgrade of the baseline would destroy the entire production
    # database with no way back. If you truly need to tear everything
    # down, do it explicitly and deliberately, not via `alembic downgrade`.
    pass
