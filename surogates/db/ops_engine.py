"""Read-only async engine + session factory for the surogate-ops DB.

The runtime KB tools (kb_list_pages, kb_read_page) read three tables
out of ops:

  - knowledge_bases     -- which KBs exist + their metadata
  - kb_wiki_pages       -- the wiki page tree per KB
  - agent_knowledge_bases -- M2M telling us which KBs this agent owns

We never write to ops from this side. The compile-time pipeline (in
surogate-ops itself) owns all writes; runtime is read-only by design,
which lets us hand the worker pod credentials with SELECT-only grants
in a hardened deployment.

The engine is created lazily and cached at module level so repeated
calls don't pile up connections.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)


_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def init_ops_engine(
    url: str,
    *,
    pool_size: int = 2,
    pool_overflow: int = 2,
) -> async_sessionmaker[AsyncSession]:
    """Initialize the ops DB engine + session factory once per process.

    Idempotent: calling it again with the same URL returns the cached
    factory; calling it with a different URL is a programming error
    (we log and replace).
    """
    global _engine, _session_factory

    if _engine is not None and _session_factory is not None:
        if str(_engine.url) == url:
            return _session_factory
        logger.warning(
            "init_ops_engine called with different URL; replacing cached engine",
        )

    engine = create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_overflow,
        pool_pre_ping=True,
    )
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    _engine = engine
    _session_factory = factory
    logger.info("Ops DB engine initialized")
    return factory


def get_ops_session_factory() -> Optional[async_sessionmaker[AsyncSession]]:
    """Return the cached ops session factory, or None if not initialized.

    None is the signal "KB tools disabled" -- callers should skip
    KB-related work entirely rather than fail at session-open time.
    """
    return _session_factory
