"""Fleet-backed browser backend.

Drops in beside ``ProcessBrowserBackend`` and ``K8sBrowserBackend`` and
delegates the lifecycle to the cluster-wide BrowserFleetManager running
in surogate-ops. The worker side stays simple: each ``provision`` is one
authenticated POST, each ``destroy`` is one mirrored POST, and the
session map in ``BrowserPool`` is unchanged. No K8s API access from the
worker is required when this backend is selected.
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

logger = logging.getLogger(__name__)


class FleetAtCapacity(RuntimeError):
    """surogate-ops returned 503 fleet_at_capacity.

    Carries the parsed body so the composite fallback can use the hint
    (e.g., ``retry_after_ms``) if it ever wants to.
    """

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("message") or "fleet at capacity")
        self.payload = payload


@dataclass
class FleetBackend:
    """Worker-side proxy to surogate-ops's /api/browser-fleet."""

    endpoint: str
    worker_token: str
    http: httpx.AsyncClient
    timeout_seconds: float = 75.0
    storage_settings: Any | None = None  # source of session S3 creds
    _leases: dict[str, str] = field(default_factory=dict, init=False)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.worker_token}"}

    def _resolve_s3_creds(self, spec: BrowserSpec) -> dict[str, Any] | None:
        """Pull S3 creds from the worker's storage settings.

        Only emitted when the session asks for a workspace mount; pure
        browsing (no workspace) doesn't need creds at all.
        """
        if not spec.workspace_source_ref:
            return None
        if self.storage_settings is None:
            raise RuntimeError(
                "FleetBackend has no storage_settings but the session "
                "asked for a workspace mount",
            )
        return {
            "access_key": getattr(self.storage_settings, "access_key", "") or "",
            "secret_key": getattr(self.storage_settings, "secret_key", "") or "",
            "region": getattr(self.storage_settings, "region", None) or None,
            "endpoint": getattr(self.storage_settings, "endpoint", None) or None,
            "session_token": getattr(self.storage_settings, "session_token", None) or None,
        }

    async def provision(
        self,
        spec: BrowserSpec,
        *,
        session_id: str,
        org_id: str,
        user_id: str,
    ) -> tuple[str, BrowserEndpoint]:
        body = {
            "session_id": session_id,
            "org_id": org_id,
            "user_id": user_id,
            "workspace_source_ref": spec.workspace_source_ref,
            "env": dict(spec.env),
            "s3_creds": self._resolve_s3_creds(spec),
        }
        r = await self.http.post(
            f"{self.endpoint}/lease",
            json=body,
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
        if r.status_code == 503:
            payload = r.json() if r.content else {}
            raise FleetAtCapacity(payload)
        r.raise_for_status()
        data = r.json()
        endpoint = BrowserEndpoint(**data["endpoint"])
        self._leases[data["browser_id"]] = data["lease_id"]
        return data["browser_id"], endpoint

    async def status(self, browser_id: str) -> BrowserStatus:
        r = await self.http.get(
            f"{self.endpoint}/pod/{browser_id}/status",
            headers=self._headers(),
            timeout=10.0,
        )
        r.raise_for_status()
        raw = r.json()["status"]
        return BrowserStatus(raw)

    async def destroy(self, browser_id: str) -> None:
        lease_id = self._leases.pop(browser_id, None)
        if lease_id is None:
            # Either destroy was called twice or the browser was provisioned
            # via a different backend (the composite fallback case).
            logger.debug(
                "destroy called for unknown browser_id %s — no-op", browser_id,
            )
            return
        try:
            await self.http.post(
                f"{self.endpoint}/release",
                json={"lease_id": lease_id, "browser_id": browser_id},
                headers=self._headers(),
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            # The fleet's reaper will clean up via activeDeadlineSeconds;
            # don't fail the session teardown on a transport hiccup.
            logger.warning(
                "release best-effort failed for %s: %s", browser_id, exc,
            )
