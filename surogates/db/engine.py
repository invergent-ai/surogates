"""Database engine and async session factory."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from surogates.config import DatabaseSettings

logger = logging.getLogger(__name__)

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


def run_migrations(db_settings: DatabaseSettings) -> None:
    """Create all tables from SQLAlchemy ORM metadata.

    For development and initial deployment this uses
    ``Base.metadata.create_all`` which is idempotent (skips existing
    tables).  A future version can wire Alembic for versioned migrations.
    """
    import asyncio

    from surogates.db.models import Base

    async def _create_all() -> None:
        engine = async_engine_from_settings(db_settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create_all())
    logger.info(
        "Database tables created/verified: %s",
        db_settings.url.rsplit("@", 1)[-1],
    )
