"""Per-process L1 + L2 cache for AgentFileBundle handles.

Same TTL + per-key lock + double-checked-
locking shape as RuntimeConfigCache, FirebaseConfigCache, SlugResolverCache.

Loader exceptions are NOT memoised: a LookupError on call N must
let call N+1 retry because an admin rollback (pushing the old
bundle version back) must land in the next session, not the next
TTL tick.

The L2 disk cache keys on (agent_id, version) and lives
under ``~/.surogate/bundle-cache/<agent_id>/<version>/``.  L1
holds the AgentFileBundle handle; L2 backs the HubBundleClient's
individual file fetches so a worker restart doesn't blast the Hub
re-pulling everything from scratch.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from surogates.runtime.bundle_accessor import AgentFileBundle

__all__ = ["FileBundleCache"]


logger = logging.getLogger(__name__)


class FileBundleCache:
    """Per-agent_id TTL cache around an async bundle loader."""

    def __init__(
        self,
        loader: Callable[[str], Awaitable[AgentFileBundle]],
        ttl_seconds: float = 30.0,
        l2: "_L2DiskCache | None" = None,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        self._l2 = l2
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
        if self._l2 is not None:
            # Best-effort: schedule the L2 drop but don't block the
            # synchronous invalidate() contract.  The invalidator
            # call site is the Redis pub/sub listener task; running
            # the rmtree in the background is fine.
            asyncio.create_task(self._l2.invalidate_agent(agent_id))

    async def _lock_for(self, agent_id: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(agent_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[agent_id] = lock
            return lock


class _L2DiskCache:
    """Per-process on-disk cache for individual bundle files.

    Layout: ``<root>/<agent_id>/<version>/<path>``.
    Each write goes through a ``.tmp`` sibling + rename so a concurrent
    reader never sees a partially-written file.

    The cache is best-effort: read failures return None (the L1
    fetches from Hub and re-writes), write failures log and proceed
    (cache miss next time, no data loss).
    """

    def __init__(
        self, *, root: Path, max_bytes: int = 5_000_000_000,
    ) -> None:
        self._root = root
        self._max = max_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    async def read(
        self, agent_id: str, version: str, path: str,
    ) -> bytes | None:
        target = self._target(agent_id, version, path)
        if not target.exists():
            return None
        try:
            return await asyncio.to_thread(target.read_bytes)
        except OSError:
            return None

    async def write(
        self, agent_id: str, version: str, path: str, data: bytes,
    ) -> None:
        target = self._target(agent_id, version, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")

        def _do_write() -> None:
            tmp.write_bytes(data)
            os.replace(tmp, target)

        try:
            await asyncio.to_thread(_do_write)
        except OSError:
            # Best-effort: log and proceed; next read will re-fetch.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning(
                "L2 bundle cache write failed for %s/%s/%s; "
                "next read will re-fetch from Hub",
                agent_id, version, path, exc_info=True,
            )

    async def invalidate_agent(self, agent_id: str) -> None:
        """Drop every cached version for ``agent_id``.

        Called from the L1 invalidate hook so an admin rollback to
        any prior version forces a fresh fetch instead of replaying
        whichever version happens to still be on disk."""
        target = self._root / agent_id
        if not target.exists():
            return
        await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)

    def _target(self, agent_id: str, version: str, path: str) -> Path:
        # Path can contain forward slashes (e.g. "skills/foo/SKILL.md");
        # join carefully so we don't escape the agent_id directory.
        clean = path.lstrip("/")
        return self._root / agent_id / version / clean


class _L2ReadThroughHub:
    """Read-through L2 wrapper over a HubBundleClient.

    AgentFileBundle.read_bytes calls
    ``self.client.read_bytes(version, path)``; when ``self.client``
    is this wrapper, individual file reads check the on-disk L2
    cache first, fall back to the Hub on miss, and write-through
    to L2 on success.

    Surface matches HubBundleClient exactly so AgentFileBundle
    can't tell the difference.  ``list_paths`` is forwarded verbatim
    because directory listings aren't cached at L2 — they're cheap
    to recompute on the few-objects-per-prefix scale that bundle
    layouts have.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        hub: Any,
        l2: _L2DiskCache,
    ) -> None:
        self._agent_id = agent_id
        self._hub = hub
        self._l2 = l2

    async def read_bytes(self, ref: str, path: str) -> bytes:
        cached = await self._l2.read(self._agent_id, ref, path)
        if cached is not None:
            return cached
        data = await self._hub.read_bytes(ref, path)
        await self._l2.write(self._agent_id, ref, path, data)
        return data

    async def list_paths(self, ref: str, *, prefix: str = "") -> list[str]:
        return await self._hub.list_paths(ref, prefix=prefix)

    async def aclose(self) -> None:
        await self._hub.aclose()
