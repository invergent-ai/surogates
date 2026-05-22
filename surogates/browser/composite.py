"""Composite browser backend with fallback semantics.

Pairs a primary backend (typically ``FleetBackend``) with a fallback
(typically ``K8sBrowserBackend`` or ``ProcessBrowserBackend``). When the
primary returns ``FleetAtCapacity`` or a transport-level error, the
composite transparently provisions via the fallback so a fleet outage
or capacity-exhaustion event degrades to today's per-session
behaviour rather than failing the lease outright.

The composite maintains an in-memory ``browser_id → backend`` routing
table so ``destroy`` and ``status`` calls reach the backend that
originally provisioned the browser. After a worker restart the table
is empty; subsequent ``destroy`` calls for unknown browser_ids default
to the primary (the fleet's release endpoint is idempotent on unknown
lease ids).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from surogates.browser.base import (
    BrowserBackend,
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
)
from surogates.browser.fleet import FleetAtCapacity

logger = logging.getLogger(__name__)


@dataclass
class CompositeFallbackBackend:
    primary: BrowserBackend
    fallback: BrowserBackend
    _routing: dict[str, BrowserBackend] = field(default_factory=dict, init=False)

    async def provision(
        self,
        spec: BrowserSpec,
        *,
        session_id: str,
        org_id: str,
        user_id: str,
    ) -> tuple[str, BrowserEndpoint]:
        try:
            browser_id, endpoint = await self.primary.provision(
                spec,
                session_id=session_id,
                org_id=org_id,
                user_id=user_id,
            )
            self._routing[browser_id] = self.primary
            return browser_id, endpoint
        except (FleetAtCapacity, httpx.RequestError) as exc:
            logger.warning(
                "primary browser backend unavailable (%s); using fallback "
                "for session %s",
                exc,
                session_id,
            )
            browser_id, endpoint = await self.fallback.provision(
                spec,
                session_id=session_id,
                org_id=org_id,
                user_id=user_id,
            )
            self._routing[browser_id] = self.fallback
            return browser_id, endpoint

    async def status(self, browser_id: str) -> BrowserStatus:
        backend = self._routing.get(browser_id, self.primary)
        return await backend.status(browser_id)

    async def destroy(self, browser_id: str) -> None:
        backend = self._routing.pop(browser_id, self.primary)
        await backend.destroy(browser_id)
