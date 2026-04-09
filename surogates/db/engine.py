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
        connect_args={
            "statement_cache_size": 0,  # avoid asyncpg prepared-statement leaks
        },
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
    """Run Alembic migrations programmatically.

    This is a deliberate placeholder: Alembic configuration (env.py, versions
    directory, alembic.ini) is managed separately.  Calling this function logs
    a warning until the migration infrastructure is wired up.
    """
    logger.warning(
        "run_migrations() called but Alembic is not yet wired; "
        "database URL: %s",
        db_settings.url.rsplit("@", 1)[-1],  # log host/db only, not credentials
    )
