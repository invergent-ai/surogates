"""HTTP client for the management plane's per-agent runtime config.

Wraps a single long-lived ``httpx.AsyncClient`` so
the api / worker processes hold exactly one connection pool per pod
toward surogate-ops.  Callers go through
:class:`surogates.runtime.cache.RuntimeConfigCache` — the
client is the cache's loader, not the per-request entry point.

Error taxonomy used by the cache + the upstream resolver:

* :class:`LookupError` — surogate-ops returned 404.  The agent does
  not exist in the management plane, *or* the management plane refuses
  to serve it because ``runtime_kind != shared``.  The resolver maps
  this to a 404 toward the runtime caller.
* :class:`PlatformAuthError` — surogate-ops returned 401.  Our bearer
  token is bad or has been revoked.  Operations problem; emit a metric
  / page rather than retry.  The resolver maps this to a 503 toward
  the caller and bubbles a structured log line.
* ``httpx.HTTPStatusError`` — every other non-2xx (typically 5xx).
  The cache layer may decide to serve a stale entry if it has one.

We deliberately do *not* swallow these; the cache layer is the only
component allowed to interpret them.
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = ["PlatformAuthError", "PlatformClient"]


class PlatformAuthError(RuntimeError):
    """Surogate-ops rejected our bearer token (401)."""


class PlatformClient:
    """Async HTTP client for surogate-ops.

    Hold one instance on ``app.state.platform_client`` for the api/worker
    process lifetime.  ``aclose()`` shuts down the underlying httpx
    connection pool — call it from the FastAPI lifespan / worker
    teardown so connections do not linger across replicas.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "timeout": timeout,
            "headers": {"Authorization": f"Bearer {token}"},
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    async def get_runtime_config(self, agent_id: str) -> dict:
        """Fetch the agent's runtime-config payload.

        Returns the raw JSON dict; the projection into
        :class:`~surogates.runtime.context.AgentRuntimeContext` is the
        resolver's job.
        """
        try:
            resp = await self._client.get(
                # surogate-ops mounts the agent_runtime_router at
                # ``/api/agents`` AND the route itself is declared with
                # ``/agents/{agent_id}/runtime-config`` -- so the actual
                # URL is the doubled prefix.  Studio + every other ops
                # client uses this shape; we match it here.
                f"/api/agents/agents/{agent_id}/runtime-config",
            )
        except httpx.HTTPError as exc:
            # Network-level failure (DNS, refused, timeout, etc.).  We
            # surface these unchanged so the cache layer can apply its
            # stale-on-failure policy.
            raise exc

        if resp.status_code == 404:
            raise LookupError(
                f"agent {agent_id} not configured for shared runtime",
            )
        if resp.status_code == 401:
            raise PlatformAuthError(
                "surogate-ops rejected runtime token (401); "
                "is the token revoked or missing the 'runtime' scope?",
            )
        resp.raise_for_status()
        return resp.json()

    async def get_firebase_config(self, project_id: str) -> dict:
        """Fetch the per-project Firebase web config.

        Returns the raw JSON dict; projection into
        :class:`~surogates.runtime.firebase.FirebaseConfig` is the
        cache loader's job.  Raises:

        * :class:`LookupError` on 404 — the project exists but has no
          BYO Firebase configured.  The cache surfaces this to the
          login route, which falls back to platform-default auth.
        * :class:`PlatformAuthError` on 401 — runtime token is bad or
          revoked.  Operations problem.
        * ``httpx.HTTPStatusError`` on any other non-2xx.
        """
        try:
            resp = await self._client.get(
                f"/api/projects/{project_id}/firebase-config",
            )
        except httpx.HTTPError as exc:
            raise exc

        if resp.status_code == 404:
            raise LookupError(
                f"project {project_id} has no Firebase config",
            )
        if resp.status_code == 401:
            raise PlatformAuthError(
                "surogate-ops rejected runtime token (401); "
                "is the token revoked or missing the 'runtime' scope?",
            )
        resp.raise_for_status()
        return resp.json()

    async def get_agent_id_for_slug(self, slug: str) -> str | None:
        """Resolve a DNS-safe agent slug to its agent_id.

        Returns ``None`` (not :class:`LookupError`) on 404 because slug
        misses are a common, expected case — the Host-header resolver
        checks slugs on every incoming request including reserved
        subdomains like ``www.`` / ``api.`` that the caller already
        filtered out.  ``None`` lets the resolver write a single
        branch.

        * :class:`PlatformAuthError` on 401 — operations problem.
        * ``httpx.HTTPStatusError`` on any other non-2xx — the cache
          layer may decide to serve a stale entry if it has one.
        """
        try:
            resp = await self._client.get(f"/api/agents/by-slug/{slug}")
        except httpx.HTTPError as exc:
            raise exc

        if resp.status_code == 404:
            return None
        if resp.status_code == 401:
            raise PlatformAuthError(
                "surogate-ops rejected runtime token (401); "
                "is the token revoked or missing the 'runtime' scope?",
            )
        resp.raise_for_status()
        return resp.json()["agent_id"]

    async def get_channel_routing(
        self, kind: str, identifier: str,
    ) -> dict | None:
        """Fetch the routing record for an inbound channel event.

        Returns the JSON dict (``{"org_id", "agent_id", "api_web_url"}``)
        when a row exists in the ``channel_routing`` table; returns
        ``None`` on 404 (no routing configured for this identifier --
        a normal state) so the :class:`ChannelRoutingCache`'s
        negative-memoise path is the single code path.

        * :class:`PlatformAuthError` on 401 -- operations problem.
        * ``httpx.HTTPStatusError`` on any other non-2xx.
        """
        try:
            resp = await self._client.get(
                f"/api/channels/by-identifier/{kind}/{identifier}",
            )
        except httpx.HTTPError as exc:
            raise exc

        if resp.status_code == 404:
            return None
        if resp.status_code == 401:
            raise PlatformAuthError(
                "surogate-ops rejected runtime token (401); "
                "is the token revoked or missing the 'runtime' scope?",
            )
        resp.raise_for_status()
        return resp.json()

    async def mint_composio_session(
        self, agent_id: str, user_id: str,
    ) -> dict | None:
        """Mint a per-end-user Composio Tool Router MCP server for an agent.

        Returns the minted ``{transport, url, headers}`` config, or
        ``None`` when the agent has no usable Composio toolkits (404) or
        Composio is not configured on surogate-ops (503).  The returned
        headers carry the Composio ``x-api-key`` — a runtime secret.
        Keep it in memory only; never log it.

        * :class:`PlatformAuthError` on 401 -- operations problem.
        * ``httpx.HTTPStatusError`` on any other non-2xx.
        """
        try:
            resp = await self._client.post(
                f"/api/agents/agents/{agent_id}/composio/session",
                json={"user_id": user_id},
            )
        except httpx.HTTPError as exc:
            raise exc

        if resp.status_code in (404, 503):
            return None
        if resp.status_code == 401:
            raise PlatformAuthError(
                "surogate-ops rejected runtime token (401); "
                "is the token revoked or missing the 'runtime' scope?",
            )
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()
