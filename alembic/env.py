"""
SmAttaker — Alembic Migration Environment
Async-compatible: reuses the same DATABASE_URL and Base.metadata the
running application uses (backend.database / backend.config), so
there's exactly one source of truth for the schema instead of a
separate connection string to keep in sync by hand.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ── Import the app's models so autogenerate can see them ───
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.database import Base
from backend.config import settings

# Import every model module so its tables register on Base.metadata —
# same list as backend/database.py's init_db(), kept here too since
# Alembic's autogenerate needs them imported at the time it inspects
# Base.metadata, independent of whether the app has started yet.
import backend.models.signal              # noqa: F401
import backend.models.user                # noqa: F401
import backend.models.trade               # noqa: F401
import backend.models.subscription        # noqa: F401
import backend.models.risk_settings       # noqa: F401
import backend.models.exchange_connection  # noqa: F401
import backend.models.admin_settings      # noqa: F401
import backend.models.admin_notification  # noqa: F401
import backend.models.admin_audit_log     # noqa: F401

config = context.config

# Feed the live DATABASE_URL from app settings into Alembic's config,
# rather than requiring it duplicated in alembic.ini.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL script without a live DB connection (`--sql` mode)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database using the async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
