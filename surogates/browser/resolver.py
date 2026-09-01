"""Resolve a session's browser endpoint for API-side proxying."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from surogates.browser.base import BrowserEndpoint
from surogates.browser.registry import BrowserEntry, BrowserRegistry

logger = logging.getLogger(__name__)


class _BackendWithFind(Protocol):
    async def find_entry_by_session(self, session_id: str) -> BrowserEntry | None:
        """Return browser metadata reconstructed from backend state."""
        ...


@dataclass(slots=True)
class ResolvedBrowser:
    """Tenant-checked browser endpoint returned to API routes."""

    session_id: str
    endpoint: BrowserEndpoint
    org_id: str | None = None
    user_id: str | None = None
    source: str = "registry"


class BrowserResolver:
    """Resolve browser metadata from Redis first, then backend labels.

    Redis is the fast path written by workers when a browser is provisioned.
    The backend fallback exists for registry loss or eviction, but it must
    preserve tenant metadata so a registry miss cannot leak another tenant's
    browser endpoint.
    """

    def __init__(
        self,
        *,
        registry: BrowserRegistry,
        backend: _BackendWithFind | None,
    ) -> None:
        self._registry = registry
        self._backend = backend

    async def forget_unreachable(self, session_id: str) -> None:
        """Drop a registry entry a caller has just proven it cannot reach.

        Nothing else prunes this map: a browser can die without the server
        emitting ``browser.destroyed``, and the entry then outlives it
        indefinitely. Every consumer that trusts ``resolve`` — the preview
        endpoint, the browser shell, the UI deciding whether to offer a
        browser at all — is left discovering the truth by timing out.

        A request that has just failed to reach the endpoint is proof the
        entry is wrong now, so removing it is safe and makes the next
        ``resolve`` answer honestly. Never raises: this runs on a path that
        is already returning an error, and a second failure there would
        replace a clear browser error with a confusing registry one.
        """

        try:
            await self._registry.delete(session_id)
        except Exception:  # noqa: BLE001 - a courtesy, never the caller's problem
            logger.warning(
                "Could not prune unreachable browser %s from the registry",
                session_id,
                exc_info=True,
            )

    async def resolve(
        self,
        session_id: str,
        *,
        expected_org_id: str | None,
    ) -> ResolvedBrowser | None:
        entry = await self._registry.get(session_id)
        if entry is not None:
            return self._from_entry(
                entry,
                expected_org_id=expected_org_id,
                source="registry",
            )

        if self._backend is None:
            return None

        fallback_entry = await self._backend.find_entry_by_session(session_id)
        if fallback_entry is None:
            return None
        return self._from_entry(
            fallback_entry,
            expected_org_id=expected_org_id,
            source="k8s_fallback",
        )

    def _from_entry(
        self,
        entry: BrowserEntry,
        *,
        expected_org_id: str | None,
        source: str,
    ) -> ResolvedBrowser | None:
        if expected_org_id is not None and entry.org_id != expected_org_id:
            logger.warning(
                "Browser %s hit for session %s but org %s != expected %s",
                source,
                entry.session_id,
                entry.org_id,
                expected_org_id,
            )
            return None

        return ResolvedBrowser(
            session_id=entry.session_id,
            endpoint=BrowserEndpoint(
                rest_url=entry.rest_url,
                cdp_url=entry.cdp_url,
                live_view_url=entry.live_view_url,
            ),
            org_id=entry.org_id,
            user_id=entry.user_id,
            source=source,
        )
