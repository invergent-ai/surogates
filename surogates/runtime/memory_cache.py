"""Per-process L1 cache for per-user memory bytes.

Same TTL + per-key-lock + double-checked-locking
shape as RuntimeConfigCache / FirebaseConfigCache /
SlugResolverCache / FileBundleCache.

Key shape: ``"<org_id>:<user_id>"`` verbatim — the 
invalidator channel ``user.memory_changed:<org_id>:<user_id>``
passes the colon-joined string through to ``cache.invalidate``
without a parser, so the cache key shape and the channel
identifier shape are deliberately the same string.

Loader exceptions are NOT memoised: a transient R2 failure on
call N must let call N+1 retry instead of poisoning the cache for
the full TTL window.

L2 is intentionally absent.  Bundles cache to disk
because bundle contents are large and pre-existing for a given
``(agent_id, version)`` tuple; user memory is small, write-heavy,
and tied to a session lifetime — a disk cache would add code
without buying meaningful latency.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

__all__ = ["MemoryCache"]


class MemoryCache:
    """Per-process TTL cache around an async memory loader."""

    def __init__(
        self,
        loader: Callable[[str], Awaitable[bytes]],
        ttl_seconds: float = 5.0,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, bytes]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    async def get(self, key: str) -> bytes:
        cached = self._entries.get(key)
        if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
            return cached[1]

        lock = await self._lock_for(key)
        async with lock:
            cached = self._entries.get(key)
            if (
                cached is not None
                and (time.monotonic() - cached[0]) < self._ttl
            ):
                return cached[1]
            data = await self._loader(key)
            self._entries[key] = (time.monotonic(), data)
            return data

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
