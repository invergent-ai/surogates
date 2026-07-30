"""Per-process TTL cache for agent runtime configs.

Pure cache fronting :class:`~surogates.runtime.PlatformClient`.  The
management plane is the source of truth; the cache exists to absorb
read load on the hot path.  Eviction policies:

* **TTL** — every entry expires ``ttl_seconds`` after its load, but a
  transient loader failure may serve the expired entry for up to
  ``stale_grace_seconds`` longer (stale-on-failure, see :meth:`get`).
* **Explicit invalidate** — :meth:`invalidate` drops a single key,
  driven by the Redis pub/sub listener when surogate-ops
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
import logging
import time
from typing import Awaitable, Callable

from surogates.runtime.platform_client import PlatformAuthError

logger = logging.getLogger(__name__)

# Sentinel far in the monotonic past so "never failed" never matches
# the backoff window.
_FAR_PAST = 1e12

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
        stale_grace_seconds: float = 300.0,
        failure_backoff_seconds: float = 2.0,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._stale_grace = stale_grace_seconds
        self._failure_backoff = failure_backoff_seconds
        self._entries: dict[str, tuple[float, dict]] = {}
        self._last_failure: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    async def get(self, agent_id: str) -> dict:
        """Return the cached config, fetching through the loader on miss.

        Stale-on-failure: a transient loader error (ops redeploying,
        network blip, a version bump racing this request) serves the
        expired entry for up to ``stale_grace_seconds`` past its TTL
        instead of failing the request — the platform client surfaces
        network errors unchanged for exactly this policy. After a
        failure, further calls within ``failure_backoff_seconds`` serve
        the stale copy WITHOUT re-entering the loader, so an ops outage
        does not serialize every request behind a timing-out fetch;
        past the backoff the loader is retried (the stale timestamp is
        never refreshed). Two errors are definitive and never absorbed:
        ``LookupError`` (404 — the agent is gone; also purges the entry
        so a deleted agent cannot be served stale) and
        ``PlatformAuthError`` (the runtime token is rejected — a
        condition that never self-heals and must page, not retry).
        """
        now = time.monotonic()
        cached = self._entries.get(agent_id)
        if cached is not None and (now - cached[0]) < self._ttl:
            return cached[1]
        if (
            cached is not None
            and (now - cached[0]) < self._ttl + self._stale_grace
            and (now - self._last_failure.get(agent_id, -_FAR_PAST))
            < self._failure_backoff
        ):
            return cached[1]

        lock = await self._lock(agent_id)
        async with lock:
            # Double-checked after taking the lock — a peer may have
            # already loaded while we waited.
            cached = self._entries.get(agent_id)
            if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
                return cached[1]
            try:
                cfg = await self._loader(agent_id)
            except LookupError:
                self._entries.pop(agent_id, None)
                self._last_failure.pop(agent_id, None)
                raise
            except PlatformAuthError:
                raise
            except Exception:
                self._last_failure[agent_id] = time.monotonic()
                if (
                    cached is not None
                    and (time.monotonic() - cached[0])
                    < self._ttl + self._stale_grace
                ):
                    logger.warning(
                        "runtime-config refresh failed for agent %s — "
                        "serving the cached copy",
                        agent_id,
                        exc_info=True,
                    )
                    return cached[1]
                raise
            self._last_failure.pop(agent_id, None)
            self._entries[agent_id] = (time.monotonic(), cfg)
            return cfg

    def invalidate(self, agent_id: str) -> None:
        """Drop the cache entry for ``agent_id`` if present.

        Safe to call for unknown keys; pop default-None makes it a
        no-op.
        """
        self._entries.pop(agent_id, None)

    async def _lock(self, agent_id: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(agent_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[agent_id] = lock
            return lock
