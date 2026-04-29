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

# Path to the KB DDL (pgvector ext, GENERATED tsvector, HNSW + GIN indexes,
# RLS policies).  Same rationale as observability.sql — the constructs here
# are not expressible via SQLAlchemy DDL.  See ``apply_kb_ddl`` below.
KB_SQL_PATH = Path(__file__).with_name("kb.sql")

# Suppress noisy CancelledError logs from the connection pool when SSE
# clients disconnect mid-query.  These are harmless — the pool discards
# the cancelled connection and creates a fresh one.
logging.getLogger("sqlalchemy.pool.impl").setLevel(logging.CRITICAL)


def async_engine_from_settings(db_settings: DatabaseSettings) -> AsyncEngine:
    """Create an :class:`AsyncEngine` from application database settings.

    The engine is configured with connection-pool parameters drawn from
    *db_settings* and uses sensible production defaults (pool pre-ping
    enabled, prepared-statement caching via asyncpg ``statement_cache_size``).
    """
    return create_async_engine(
        db_settings.url,
        pool_size=db_settings.pool_size,
        max_overflow=db_settings.pool_overflow,
        pool_pre_ping=True,
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


async def apply_observability_ddl(conn: AsyncConnection) -> None:
    """Apply the observability trigger + views on an open connection.

    Reads :data:`OBSERVABILITY_SQL_PATH` and runs it through the
    underlying asyncpg connection's simple-query protocol, which is the
    only path that accepts a multi-statement script.  Every statement
    in the file is idempotent (``CREATE OR REPLACE`` / ``DROP IF
    EXISTS``) so it is safe to run on every startup.  Callers must
    pass a connection already inside a transaction (e.g. ``async with
    engine.begin() as conn``).
    """
    sql = OBSERVABILITY_SQL_PATH.read_text(encoding="utf-8")
    # asyncpg's execute() accepts multi-statement scripts when called
    # without parameters (simple query protocol).  SQLAlchemy's
    # ``exec_driver_sql`` uses the extended protocol and rejects them.
    raw = await conn.get_raw_connection()
    await raw.driver_connection.execute(sql)


async def apply_kb_ddl(conn: AsyncConnection) -> None:
    """Apply the KB DDL: pgvector extension, GENERATED tsvector column on
    ``kb_chunk``, GIN + HNSW indexes, and row-level-security policies.

    Reads :data:`KB_SQL_PATH` via the simple-query protocol like
    :func:`apply_observability_ddl`.  Idempotent.

    Note: the pgvector extension is also installed by :func:`run_migrations`
    *before* ``Base.metadata.create_all`` — that path matters because the
    ORM emits ``embedding vector(1024)`` and the type must exist when the
    table is created on a fresh database.  The ``CREATE EXTENSION`` here
    is a safety net for re-runs.
    """
    sql = KB_SQL_PATH.read_text(encoding="utf-8")
    raw = await conn.get_raw_connection()
    await raw.driver_connection.execute(sql)


def run_migrations(db_settings: DatabaseSettings) -> None:
    """Create all tables and install observability + KB DDL.

    Uses ``Base.metadata.create_all`` (idempotent — skips existing
    tables) for ORM-managed schema, then applies
    :func:`apply_observability_ddl` and :func:`apply_kb_ddl`.  The
    pgvector extension is installed *before* ``create_all`` because the
    KB ORM emits ``vector(1024)`` and the type must exist before the
    table is created on a fresh database.  A future version can wire
    Alembic for versioned migrations.
    """
    import asyncio

    from surogates.db.models import Base

    async def _create_all() -> None:
        engine = async_engine_from_settings(db_settings)
        async with engine.begin() as conn:
            # Install pgvector before create_all — the kb_chunk.embedding
            # column is ``vector(1024)`` and the type must exist on a fresh DB.
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(
                "CREATE EXTENSION IF NOT EXISTS vector;"
            )
            await conn.run_sync(Base.metadata.create_all)
            await apply_observability_ddl(conn)
            await apply_kb_ddl(conn)
        await engine.dispose()

    asyncio.run(_create_all())
    logger.info(
        "Database tables + observability + KB DDL created/verified: %s",
        db_settings.url.rsplit("@", 1)[-1],
    )
