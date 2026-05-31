"""Per-process TTL cache for project Firebase configs.

Mirrors :class:`~surogates.runtime.RuntimeConfigCache`
but keyed on ``project_id``.  Default TTL is longer (60 s) because
Firebase config changes rarely.  Invalidated via the
``project.firebase_config_changed:<project_id>`` Redis pub/sub channel.

Loader exceptions are *not* memoised.  A 404 (project has no Firebase
configured) on call N must let call N+1 retry — projects can adopt
Firebase between requests and a cached negative would block adoption
for an entire TTL window.

The cache stores :class:`FirebaseConfig` dataclass instances, NOT the
raw JSON dict.  The factory that constructs the cache wraps the
management-plane HTTP loader with the dict → dataclass projection so
every caller of :meth:`get` sees a typed object.  This is the runtime-
side equivalent of how :class:`RuntimeConfigCache` returns the raw dict
because the resolver projects it into ``AgentRuntimeContext`` at a
later step — Firebase has only one consumer, so the projection lives
inline.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from surogates.runtime.firebase import FirebaseConfig

__all__ = ["FirebaseConfigCache"]


class FirebaseConfigCache:
    """In-process LRU-ish cache keyed by ``project_id``.

    Same structural shape as RuntimeConfigCache; only the default TTL
    differs (60 s instead of 1 s) because Firebase config mutations are
    rare — when they happen the Redis pub/sub invalidation tick drops
    the entry immediately, so the TTL is just an upper bound.
    """

    def __init__(
        self,
        loader: Callable[[str], Awaitable[FirebaseConfig]],
        ttl_seconds: float = 60.0,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, FirebaseConfig]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    async def get(self, project_id: str) -> FirebaseConfig:
        now = time.monotonic()
        cached = self._entries.get(project_id)
        if cached is not None and (now - cached[0]) < self._ttl:
            return cached[1]

        lock = await self._lock(project_id)
        async with lock:
            cached = self._entries.get(project_id)
            if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
                return cached[1]
            cfg = await self._loader(project_id)
            self._entries[project_id] = (time.monotonic(), cfg)
            return cfg

    def invalidate(self, project_id: str) -> None:
        """Drop the cache entry for ``project_id`` if present."""
        self._entries.pop(project_id, None)

    def invalidate_all(self) -> None:
        """Drop every entry — used on pod shutdown for a clean restart."""
        self._entries.clear()

    async def _lock(self, project_id: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(project_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[project_id] = lock
            return lock
