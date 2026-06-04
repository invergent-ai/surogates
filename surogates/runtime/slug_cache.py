"""Per-process TTL cache for slug → agent_id resolutions.

Slugs change rarely (renaming an agent is a
deliberate admin action) so the default TTL is 30 s — longer than the
runtime-config cache (1 s) but shorter than the Firebase cache (60 s).

Negative entries (slug → ``None``) are memoised explicitly via a
sentinel so reserved-subdomain probes (``www.``, ``api.``, ``status.``,
etc.) do not hit the management plane on every request.  Loader
exceptions, in contrast, are *not* memoised: they represent unknown
state and the next call must retry.

Concurrent gets for the same fresh slug are deduplicated through a
per-key :class:`asyncio.Lock` so a flood of cold requests results in a
single platform call.  Double-checked locking inside the lock catches
the case where another waiter just populated the entry while we
queued.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

__all__ = ["SlugResolverCache"]


class SlugResolverCache:
    """TTL cache around an async slug → agent_id loader.

    ``ttl_seconds`` is the upper bound on staleness; explicit
    :meth:`invalidate` from the Redis pub/sub channel
    ``agent.slug_changed:`` lets the management plane drop entries
    immediately on rename so the TTL is in practice only the
    fail-soft bound.
    """

    _MISS_SENTINEL: object = object()

    def __init__(
        self,
        loader: Callable[[str], Awaitable[str | None]],
        ttl_seconds: float = 30.0,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    async def get(self, slug: str) -> str | None:
        cached = self._entries.get(slug)
        if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
            return None if cached[1] is self._MISS_SENTINEL else cached[1]  # type: ignore[return-value]

        lock = await self._lock_for(slug)
        async with lock:
            cached = self._entries.get(slug)
            if (
                cached is not None
                and (time.monotonic() - cached[0]) < self._ttl
            ):
                return (
                    None
                    if cached[1] is self._MISS_SENTINEL
                    else cached[1]  # type: ignore[return-value]
                )
            resolved = await self._loader(slug)
            self._entries[slug] = (
                time.monotonic(),
                self._MISS_SENTINEL if resolved is None else resolved,
            )
            return resolved

    def invalidate(self, slug: str) -> None:
        self._entries.pop(slug, None)

    async def _lock_for(self, slug: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(slug)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[slug] = lock
            return lock
