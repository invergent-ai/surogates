"""Per-process L1 + L2 cache for AgentFileBundle handles.

Plan 3 / Tasks 6+7.  Same TTL + per-key lock + double-checked-
locking shape as RuntimeConfigCache (Plan 1), FirebaseConfigCache
(Plan 1b), SlugResolverCache (Plan 1b).

Loader exceptions are NOT memoised: a LookupError on call N must
let call N+1 retry because an admin rollback (pushing the old
bundle version back) must land in the next session, not the next
TTL tick.

The L2 disk cache (Task 7) keys on (agent_id, version) and lives
under ``~/.surogate/bundle-cache/<agent_id>/<version>/``.  L1
holds the AgentFileBundle handle; L2 backs the HubBundleClient's
individual file fetches so a worker restart doesn't blast the Hub
re-pulling everything from scratch.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TYPE_CHECKING

from surogates.runtime.bundle_accessor import AgentFileBundle

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["FileBundleCache"]


class FileBundleCache:
    """Per-agent_id TTL cache around an async bundle loader."""

    def __init__(
        self,
        loader: Callable[[str], Awaitable[AgentFileBundle]],
        ttl_seconds: float = 30.0,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, AgentFileBundle]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    async def get(self, agent_id: str) -> AgentFileBundle:
        cached = self._entries.get(agent_id)
        if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
            return cached[1]

        lock = await self._lock_for(agent_id)
        async with lock:
            cached = self._entries.get(agent_id)
            if (
                cached is not None
                and (time.monotonic() - cached[0]) < self._ttl
            ):
                return cached[1]
            bundle = await self._loader(agent_id)
            self._entries[agent_id] = (time.monotonic(), bundle)
            return bundle

    def invalidate(self, agent_id: str) -> None:
        self._entries.pop(agent_id, None)

    def invalidate_all(self) -> None:
        self._entries.clear()

    async def _lock_for(self, agent_id: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(agent_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[agent_id] = lock
            return lock
