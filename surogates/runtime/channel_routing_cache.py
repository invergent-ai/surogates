"""Per-process L1 cache for channel routing records.

Key shape: ``"<channel_kind>:<channel_identifier>"``
(e.g. ``"slack:A0123ABCD"`` / ``"telegram:@my_bot"`` /
``"website:pk_abc123"``).  The invalidator channel
``channel_routing_changed:<kind>:<identifier>`` passes the key
through verbatim so admins flipping a routing row -> the inbound
handler picks up the change on the next event.

Negative lookups (``None``) ARE memoised within the TTL window --
follows the SlugResolverCache convention so a malformed
inbound event storm cannot hammer the platform endpoint.  Loader
exceptions are NOT memoised (transient DB hiccups retry).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

__all__ = ["ChannelRoutingCache"]


_MISSING = object()


class ChannelRoutingCache:
    """Per-process TTL cache around a channel-routing loader."""

    def __init__(
        self,
        loader: Callable[[str], Awaitable[dict[str, Any] | None]],
        ttl_seconds: float = 30.0,
        max_entries: int | None = None,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        # ``max_entries`` bounds memory for high-cardinality keyspaces (e.g. a
        # per-sender identity cache); the TTL bounds staleness, not size, so an
        # unbounded keyspace would otherwise grow for the process lifetime.
        # ``None`` keeps the unbounded behaviour for low-cardinality callers
        # (one entry per channel_routing / mate setting).
        self._max_entries = max_entries
        self._entries: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    async def get(self, key: str) -> dict[str, Any] | None:
        cached = self._entries.get(key)
        if (
            cached is not None
            and (time.monotonic() - cached[0]) < self._ttl
        ):
            return None if cached[1] is _MISSING else cached[1]

        lock = await self._lock_for(key)
        async with lock:
            cached = self._entries.get(key)
            if (
                cached is not None
                and (time.monotonic() - cached[0]) < self._ttl
            ):
                return None if cached[1] is _MISSING else cached[1]
            data = await self._loader(key)
            self._store(key, _MISSING if data is None else data)
            return data

    def set(self, key: str, value: Any) -> None:
        """Seed a known value so the next ``get`` is a hit, not a reload.

        Used to populate the cache with a row the caller just created (e.g. a
        freshly-provisioned identity) instead of invalidating and forcing a
        reload on the next access.
        """
        self._store(key, _MISSING if value is None else value)

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    def _store(self, key: str, stored: Any) -> None:
        self._entries[key] = (time.monotonic(), stored)
        if self._max_entries is not None:
            # FIFO eviction: dict preserves insertion order, so the first key is
            # the oldest.  Drop its lock too so ``_locks`` stays bounded.
            while len(self._entries) > self._max_entries:
                oldest = next(iter(self._entries))
                self._entries.pop(oldest, None)
                self._locks.pop(oldest, None)

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
