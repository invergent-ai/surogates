"""Per-process TTL cache for agent runtime configs (Plan 1, Task 13).

Pure cache fronting :class:`~surogates.runtime.PlatformClient`.  The
management plane is the source of truth; the cache exists to absorb
read load on the hot path.  Eviction policies:

* **TTL** — every entry expires ``ttl_seconds`` after its load.
* **Explicit invalidate** — :meth:`invalidate` drops a single key,
  driven by the Redis pub/sub listener (Task 17) when surogate-ops
  publishes ``agent.runtime_config_changed:<agent_id>``.

Concurrent misses for the same ``agent_id`` are de-duplicated through
a per-key :class:`asyncio.Lock` so a thundering herd hits surogate-ops
exactly once.  Lookups for *different* keys proceed in parallel.

Loader failures are *not* cached.  A ``LookupError`` (404 from
PlatformClient) on call N must let call N+1 light up the cache once
the underlying agent flips to ``runtime_kind=shared``; otherwise we
would need a separate negative-TTL story.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

__all__ = ["RuntimeConfigCache"]


class RuntimeConfigCache:
    """In-process LRU-ish cache keyed by ``agent_id``.

    Not strictly LRU: entries live until their TTL expires or are
    explicitly invalidated.  Memory is bounded by the working set
    size, which the resolver caps elsewhere; the cache itself trusts
    upstream to keep that set small.
    """

    def __init__(
        self,
        loader: Callable[[str], Awaitable[dict]],
        ttl_seconds: float = 1.0,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    async def get(self, agent_id: str) -> dict:
        """Return the cached config, fetching through the loader on miss."""
        now = time.monotonic()
        cached = self._entries.get(agent_id)
        if cached is not None and (now - cached[0]) < self._ttl:
            return cached[1]

        lock = await self._lock(agent_id)
        async with lock:
            # Double-checked after taking the lock — a peer may have
            # already loaded while we waited.
            cached = self._entries.get(agent_id)
            if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
                return cached[1]
            cfg = await self._loader(agent_id)
            self._entries[agent_id] = (time.monotonic(), cfg)
            return cfg

    def invalidate(self, agent_id: str) -> None:
        """Drop the cache entry for ``agent_id`` if present.

        Safe to call for unknown keys; pop default-None makes it a
        no-op.
        """
        self._entries.pop(agent_id, None)

    def invalidate_all(self) -> None:
        """Drop every entry — used on pod shutdown for a clean restart."""
        self._entries.clear()

    async def _lock(self, agent_id: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(agent_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[agent_id] = lock
            return lock
