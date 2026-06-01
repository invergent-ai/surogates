"""HTTP client for the MCP proxy service.

Used by the worker when ``mcp_proxy_url`` is configured.  Discovers MCP
tools for a tenant via ``POST /mcp/v1/tools/list``, registers them into
the local ``ToolRegistry``, and forwards tool calls via
``POST /mcp/v1/tools/call``.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import httpx

from surogates.tenant.auth.jwt import create_sandbox_token
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


class McpProxyClient:
    """HTTP client that proxies MCP tool calls through the MCP proxy service.

    Parameters
    ----------
    base_url:
        The MCP proxy service URL (e.g. ``http://mcp-proxy:8001``).
    registry:
        The worker's ``ToolRegistry`` to register discovered tools into.
    """

    def __init__(self, base_url: str, registry: ToolRegistry) -> None:
        self._base_url = base_url.rstrip("/")
        self._registry = registry
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=120)
        self._discovered: set[str] = set()

    async def discover_and_register(
        self,
        org_id: UUID,
        user_id: UUID,
        session_id: UUID,
        *,
        is_service_account: bool = False,
    ) -> list[str]:
        """Discover MCP tools from the proxy and register them locally.

        Returns the list of registered tool names.

        ``is_service_account`` flags the principal so the proxy can skip
        ``users.id`` foreign keys (e.g. ``audit_log``); pass ``True``
        when ``session.user_id`` is ``None`` and we fell back to the
        session's ``service_account_id``.
        """
        token = create_sandbox_token(
            org_id, user_id, session_id,
            is_service_account=is_service_account,
        )
        headers = {"Authorization": f"Bearer {token}"}

        resp = await self._client.post("/mcp/v1/tools/list", headers=headers)
        if resp.status_code != 200:
            logger.warning(
                "MCP proxy tool discovery failed: %d %s",
                resp.status_code, resp.text[:200],
            )
            return []

        data = resp.json()
        tools = data.get("tools", [])
        registered: list[str] = []

        for tool in tools:
            name = tool.get("name", "")
            if not name or name in self._discovered:
                continue

            handler = self._make_proxy_handler(name)

            self._registry.register(
                name=name,
                schema=ToolSchema(
                    name=name,
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters", {}),
                ),
                handler=handler,
                toolset="mcp",
            )
            self._discovered.add(name)
            registered.append(name)

        if registered:
            logger.info(
                "Registered %d MCP tools via proxy: %s",
                len(registered), ", ".join(sorted(registered)),
            )

        return registered

    def _make_proxy_handler(self, tool_name: str):
        """Create a handler function that forwards tool calls to the proxy.

        The sandbox JWT is minted at call time from the active session's
        ``tenant`` + ``session_id`` rather than baked into the closure: a
        cached token expired after its 60-minute lifetime and would leak
        the first session's identity into every later session served by
        the same worker.
        """
        client = self._client

        async def handler(args: dict[str, Any], **kwargs) -> str:
            tenant = kwargs.get("tenant")
            session_id = kwargs.get("session_id")
            if tenant is None or session_id is None:
                return json.dumps({
                    "error": "MCP proxy handler missing tenant/session context",
                })
            principal_user_id = tenant.user_id or tenant.service_account_id
            if principal_user_id is None:
                return json.dumps({
                    "error": "MCP proxy handler has no principal id",
                })
            token = create_sandbox_token(
                tenant.org_id,
                principal_user_id,
                session_id,
                is_service_account=tenant.user_id is None,
            )
            headers = {"Authorization": f"Bearer {token}"}

            # Forward the chat user's identity to the upstream MCP server
            # via the MCP ``_meta`` channel. The dev-mode client builds
            # the same payload in ``surogates.tools.mcp.client``; keep
            # them in sync so platform MCP servers (e.g. surogate-ops's
            # copilot) authorise proxy-mode calls the same way they
            # authorise dev-mode calls.
            meta_payload: dict[str, Any] = {}
            session_config = kwargs.get("session_config") or {}
            ops_meta = (
                session_config.get("ops")
                if isinstance(session_config, dict) else {}
            ) or {}
            if isinstance(ops_meta, dict):
                if ops_meta.get("user_id"):
                    meta_payload["chat_user_id"] = str(ops_meta["user_id"])
                if ops_meta.get("username"):
                    meta_payload["chat_username"] = str(ops_meta["username"])
            if isinstance(ops_meta, dict) and ops_meta.get("project_id"):
                meta_payload["project_id"] = str(ops_meta["project_id"])
            elif tenant is not None and getattr(tenant, "org_id", None):
                meta_payload["project_id"] = str(tenant.org_id)
            meta_payload["session_id"] = str(session_id)

            body: dict[str, Any] = {"name": tool_name, "arguments": args}
            if meta_payload:
                body["meta"] = meta_payload

            # The proxy's call_tool route depends on
            # ``agent_runtime_context_dep`` which resolves the agent from
            # ``?agent_id=<id>`` or a Host-header subdomain.  Workers
            # talking directly to the proxy over an IP/hostname have no
            # subdomain, so propagate the harness-threaded
            # ``session.agent_id`` (see tool_exec.py:dispatch) as the
            # query param.  Falling through without it yields 400 "no
            # agent_id in request" — the symptom the platform copilot
            # sessions hit before this guard.
            agent_id = kwargs.get("agent_id")
            params: dict[str, str] | None = (
                {"agent_id": str(agent_id)} if agent_id else None
            )
            try:
                resp = await client.post(
                    "/mcp/v1/tools/call",
                    headers=headers,
                    params=params,
                    json=body,
                )
            except httpx.HTTPError as exc:
                return json.dumps({
                    "error": f"MCP proxy request failed: {exc}",
                })

            if resp.status_code != 200:
                return json.dumps({
                    "error": f"MCP proxy returned {resp.status_code}: {resp.text[:500]}",
                })

            data = resp.json()
            if data.get("error"):
                return json.dumps({"error": data["error"]})
            return data.get("result", "")

        return handler

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
