"""
SmAttaker — Database Connection & Session Management
Async PostgreSQL via SQLAlchemy 2.0 + asyncpg
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from backend.config import settings

# ── Engine ────────────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=3600,
)

# ── Session Factory ───────────────────────────────────
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Base Model ─────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Dependency: FastAPI DB Session ─────────────────────
async def get_db() -> AsyncSession:
    """Yield an async database session for FastAPI dependency injection."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Init DB (Alembic is now the primary schema mechanism) ────
async def init_db():
    """
    Create all tables, then reconcile any columns that exist on the
    SQLAlchemy models but are missing from the live database tables.

    ⚠️ UPDATE: Alembic migrations are now set up properly (see
    /alembic/versions/ and render.yaml's buildCommand, which runs
    `alembic upgrade head` on every deploy) — that's the primary
    mechanism for schema changes going forward. Use
    `alembic revision --autogenerate -m "..."` for every future model
    change instead of relying on this function.

    This function (create_all + column reconciliation) is kept as a
    defense-in-depth safety net, not the primary mechanism anymore —
    covers local dev without Alembic set up yet, and the edge case
    where a migration step didn't run for some reason. It only ADDS
    missing columns (as nullable, to avoid failing on tables that
    already have rows); it never renames, drops, or changes existing
    column types. See the original incident this was built for: a live
    `signals` table missing `entry_time` (UndefinedColumnError) because
    no migration mechanism existed at all at the time.
    """
    import backend.models.signal      # noqa: F401
    import backend.models.user        # noqa: F401
    import backend.models.trade       # noqa: F401
    import backend.models.subscription  # noqa: F401
    import backend.models.risk_settings  # noqa: F401
    import backend.models.exchange_connection  # noqa: F401
    import backend.models.admin_settings  # noqa: F401
    import backend.models.admin_notification  # noqa: F401
    import backend.models.admin_audit_log  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_reconcile_missing_columns)


def _reconcile_missing_columns(sync_conn):
    """
    Runs synchronously inside `conn.run_sync()`. Compares each model's
    declared columns against the live table's actual columns and adds
    whatever is missing via `ALTER TABLE ... ADD COLUMN`.
    """
    import logging
    from sqlalchemy import inspect, text

    logger = logging.getLogger("smattaker.database")
    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table, create_all already handled it fully

        live_columns = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in live_columns:
                continue
            try:
                col_type = column.type.compile(dialect=sync_conn.dialect)
                # Always add as nullable, regardless of the model's
                # constraint — a NOT NULL column can't be added to a
                # table that already has rows without a default, and
                # guessing a default here would be worse than just
                # logging that a manual backfill/ALTER may be needed.
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                sync_conn.execute(text(ddl))
                logger.warning(
                    f"Schema reconciliation: added missing column "
                    f"{table.name}.{column.name} ({col_type}, nullable) — "
                    f"this table was out of sync with its model. Consider "
                    f"setting up real Alembic migrations to prevent this."
                )
            except Exception as e:
                logger.error(
                    f"Schema reconciliation FAILED for {table.name}.{column.name}: {e} "
                    f"— queries touching this column will keep failing until fixed manually."
                )
