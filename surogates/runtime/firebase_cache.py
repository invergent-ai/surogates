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

__all__ = ["FirebaseConfigCache", "FirebaseConfigUnavailable", "project_firebase"]


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

    async def _lock(self, project_id: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(project_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[project_id] = lock
            return lock


class FirebaseConfigUnavailable(Exception):
    """The platform did not answer for a project's Firebase config; raised from its failure."""


async def project_firebase(cache: FirebaseConfigCache | None, project_id: str) -> FirebaseConfig | None:
    """The Firebase config of *project_id*, None when it has none or no cache is configured.

    A platform that fails to answer is not "no Firebase": a password form
    or account in a project that signs in with Firebase would be wrong.
    That raises :class:`FirebaseConfigUnavailable`, from the failure, for
    the caller to log and answer as it does.
    """
    if cache is None:
        return None
    try:
        return await cache.get(project_id)
    except LookupError:
        return None
    except Exception as exc:  # noqa: BLE001 — the platform's failures, of any kind
        raise FirebaseConfigUnavailable(project_id) from exc
