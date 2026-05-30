"""Per-process L1 cache for the per-agent MCP server registry.

Plan 5 / Task 6 + 7.  Key shape: bare ``agent_id`` (NOT
``"<org_id>:<agent_id>"`` — that's Plan 4's MemoryCache shape).

The shape diverges from MemoryCache deliberately: admins reference
agents by id when mutating the MCP server registry, so the
invalidator channel ``agent.mcp_servers_changed:<agent_id>`` only
carries the agent_id.  Routing the channel identifier straight
through to :meth:`invalidate` without a parser is the simplest
correct path, and agent ids are UUIDs in PROD so cross-org
collisions are negligible (and would over-invalidate, not under-
invalidate — strictly safe).

The cache treats keys as opaque strings; the loader is the
authoritative place that converts the key into a platform-client
call.  Loader exceptions are NOT memoised so a transient DB
hiccup doesn't poison the cache for the full TTL window — same
rule as the other Plan 1+1b+2+3+4 caches.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

__all__ = ["MCPServerRegistryCache"]


class MCPServerRegistryCache:
    """Per-process TTL cache around an async MCP server registry loader."""

    def __init__(
        self,
        loader: Callable[[str], Awaitable[list[dict]]],
        ttl_seconds: float = 30.0,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, list[dict]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    async def get(self, key: str) -> list[dict]:
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

    def invalidate_all(self) -> None:
        self._entries.clear()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
