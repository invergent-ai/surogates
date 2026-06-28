"""Database engine and async session factory."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from surogates.config import DatabaseSettings

logger = logging.getLogger(__name__)

# Path to the hand-rolled observability DDL (trigger + views).  Kept as
# a SQL file rather than SQLAlchemy DDL objects because the views contain
# PostgreSQL-specific constructs (recursive CTEs, LATERAL joins, plpgsql).
OBSERVABILITY_SQL_PATH = Path(__file__).with_name("observability.sql")

# Suppress noisy CancelledError logs from the connection pool when SSE
# clients disconnect mid-query.  These are harmless — the pool discards
# the cancelled connection and creates a fresh one.
logging.getLogger("sqlalchemy.pool.impl").setLevel(logging.CRITICAL)


def async_engine_from_settings(db_settings: DatabaseSettings) -> AsyncEngine:
    """Create an :class:`AsyncEngine` from application database settings.

    The engine is configured with connection-pool parameters drawn from
    *db_settings* and uses sensible production defaults (pool pre-ping
    enabled). For asyncpg URLs both prepared-statement caches are
    disabled so the engine is safe behind PgBouncer transaction-mode
    pooling — cached plans get invalidated when the pooler swaps the
    backend between transactions. Harmless on a direct PG connection.
    """
    connect_args: dict = {}
    if "asyncpg" in db_settings.url:
        connect_args = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        }
    return create_async_engine(
        db_settings.url,
        pool_size=db_settings.pool_size,
        max_overflow=db_settings.pool_overflow,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def async_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to *engine*.

    Sessions produced by the factory use ``expire_on_commit=False`` so that
    ORM-loaded attributes remain accessible after a commit without triggering
    lazy loads (which are not supported in async mode).
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _exec_script(conn: AsyncConnection, sql: str) -> None:
    """Run a multi-statement SQL script on an open connection.

    Uses asyncpg's simple-query protocol (the only path that accepts a
    multi-statement script — SQLAlchemy's ``exec_driver_sql`` uses the extended
    protocol and rejects them).  Scripts must be idempotent (they run on every
    startup) and the connection must already be inside a transaction (e.g.
    ``async with engine.begin() as conn``).
    """
    raw = await conn.get_raw_connection()
    await raw.driver_connection.execute(sql)


async def apply_observability_ddl(conn: AsyncConnection) -> None:
    """Apply ``observability.sql`` — the trigger, retrofit ``ALTER``s, indexes
    and views.  Every statement is idempotent (``CREATE OR REPLACE`` / ``IF
    [NOT] EXISTS`` / guarded ``DO`` blocks), so it is safe on every startup and
    is where column-level schema changes to existing tables live (no Alembic)."""
    await _exec_script(conn, OBSERVABILITY_SQL_PATH.read_text(encoding="utf-8"))


def run_migrations(db_settings: DatabaseSettings) -> None:
    """Create all tables and install observability DDL.

    Uses ``Base.metadata.create_all`` (idempotent — skips existing
    tables) for ORM-managed schema, then applies
    :func:`apply_observability_ddl` for the trigger, retrofit ``ALTER``s and
    views.  A future version can wire Alembic for versioned migrations.
    """
    import asyncio

    from surogates.db.models import Base

    async def _create_all() -> None:
        engine = async_engine_from_settings(db_settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await apply_observability_ddl(conn)
        await engine.dispose()

    asyncio.run(_create_all())
    logger.info(
        "Database tables + observability DDL created/verified: %s",
        db_settings.url.rsplit("@", 1)[-1],
    )
