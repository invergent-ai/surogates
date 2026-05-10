"""Session-scoped browser pool and lifecycle event bridge."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from surogates.browser.base import BrowserBackend, BrowserEndpoint, BrowserSpec, BrowserStatus
from surogates.browser.registry import BrowserEntry, BrowserRegistry
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

EventEmitter = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class EnsureResult:
    browser_id: str
    endpoint: BrowserEndpoint
    newly_provisioned: bool
    snapshot_cache: dict[str, dict[str, Any]]


@dataclass(slots=True)
class _Slot:
    browser_id: str
    endpoint: BrowserEndpoint
    snapshot_cache: dict[str, dict[str, Any]]


class BrowserPool:
    """Worker-local mapping from session id to provisioned browser."""

    def __init__(
        self,
        *,
        backend: BrowserBackend,
        registry: BrowserRegistry,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._emit = event_emitter
        self._mapping: dict[str, _Slot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def ensure(
        self,
        session_id: str,
        org_id: str,
        user_id: str,
        spec: BrowserSpec,
    ) -> EnsureResult:
        lock = await self._session_lock(session_id)
        async with lock:
            slot = self._mapping.get(session_id)
            if slot is not None:
                status = await self._backend.status(slot.browser_id)
                if status == BrowserStatus.RUNNING:
                    return EnsureResult(
                        browser_id=slot.browser_id,
                        endpoint=slot.endpoint,
                        newly_provisioned=False,
                        snapshot_cache=slot.snapshot_cache,
                    )
                logger.warning(
                    "Browser %s for session %s is %s; reprovisioning",
                    slot.browser_id,
                    session_id,
                    status.value,
                )
                await self._backend.destroy(slot.browser_id)
                self._mapping.pop(session_id, None)
                await self._registry.delete(session_id)

            browser_id, endpoint = await self._backend.provision(spec)
            slot = _Slot(browser_id=browser_id, endpoint=endpoint, snapshot_cache={})
            self._mapping[session_id] = slot
            await self._registry.set(
                BrowserEntry(
                    session_id=session_id,
                    org_id=org_id,
                    user_id=user_id,
                    rest_url=endpoint.rest_url,
                    cdp_url=endpoint.cdp_url,
                    live_view_url=endpoint.live_view_url,
                    provisioned_at=datetime.now(timezone.utc),
                )
            )
            await self._emit_event(
                session_id,
                EventType.BROWSER_PROVISIONED.value,
                {"session_id": session_id, "browser_id": browser_id},
            )
            return EnsureResult(
                browser_id=browser_id,
                endpoint=endpoint,
                newly_provisioned=True,
                snapshot_cache=slot.snapshot_cache,
            )

    async def destroy_for_session(self, session_id: str) -> None:
        lock = await self._session_lock(session_id)
        async with lock:
            slot = self._mapping.pop(session_id, None)
            if slot is None:
                return
            await self._backend.destroy(slot.browser_id)
            await self._registry.delete(session_id)
            await self._emit_event(
                session_id,
                EventType.BROWSER_DESTROYED.value,
                {"session_id": session_id, "browser_id": slot.browser_id},
            )

        async with self._global_lock:
            self._locks.pop(session_id, None)

    async def destroy_all(self) -> None:
        async with self._global_lock:
            session_ids = list(self._mapping.keys())
        for session_id in session_ids:
            try:
                await self.destroy_for_session(session_id)
            except Exception:
                logger.exception("Error destroying browser for session %s", session_id)

    def get_slot(self, session_id: str) -> _Slot | None:
        return self._mapping.get(session_id)

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._global_lock:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    async def _emit_event(self, session_id: str, event_type: str, data: dict[str, Any]) -> None:
        if self._emit is not None:
            await self._emit(session_id, event_type, data)
