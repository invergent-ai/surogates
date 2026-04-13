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
    ) -> list[str]:
        """Discover MCP tools from the proxy and register them locally.

        Returns the list of registered tool names.
        """
        token = create_sandbox_token(org_id, user_id, session_id)
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

        # Mint the token once for all handlers in this session.
        auth_token = create_sandbox_token(org_id, user_id, session_id)

        for tool in tools:
            name = tool.get("name", "")
            if not name or name in self._discovered:
                continue

            handler = self._make_proxy_handler(name, auth_token)

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

    def _make_proxy_handler(self, tool_name: str, auth_token: str):
        """Create a handler function that forwards tool calls to the proxy."""
        client = self._client
        headers = {"Authorization": f"Bearer {auth_token}"}

        async def handler(args: dict[str, Any], **kwargs) -> str:
            try:
                resp = await client.post(
                    "/mcp/v1/tools/call",
                    headers=headers,
                    json={"name": tool_name, "arguments": args},
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
