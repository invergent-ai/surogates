"""HTTP client for the MCP proxy service.

Used by the worker when ``mcp_proxy_url`` is configured.  Discovers MCP
tools for a tenant via ``POST /mcp/v1/tools/list``, registers them into
the local ``ToolRegistry``, and forwards tool calls via
``POST /mcp/v1/tools/call``.
"""

from __future__ import annotations

import time

import json
import logging
from typing import Any
from uuid import UUID

import httpx

from surogates.simulation.side_effects import is_simulated
from surogates.tenant.auth.jwt import create_sandbox_token
from surogates.tools.mcp.names import mcp_toolset, sanitize_mcp_name_component
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# Composio's tool-router is exposed as a single MCP server; its tools carry
# one of these server-name prefixes (the second is the non-debranded fallback).
COMPOSIO_TOOL_ROUTER_PREFIXES: tuple[str, ...] = (
    "mcp__tool_router__",
    "mcp__composio_tool_router__",
)


#: The toolset the Composio tool-router's tools are registered under
#: (``mcp-<server>``, the server being the proxy's ``composio-tool-router``).
COMPOSIO_TOOL_ROUTER_TOOLSET = "mcp-composio-tool-router"


#: Why an MCP server could not be connected, as the proxy says it (anything
#: else reads as ``error``).
UNAVAILABLE_REASONS = frozenset({"timeout", "refused", "error"})


def _unavailable_servers(data: dict[str, Any]) -> list[tuple[str, str]]:
    """The ``(server, reason)`` pairs of a tool list's ``unavailable`` field.

    The name is cut to the characters of a tool name and the reason to a
    known word: both reach the model's prompt.  A proxy without the field
    names none.
    """
    pairs: list[tuple[str, str]] = []
    for item in data.get("unavailable") or ():
        if not isinstance(item, dict):
            continue
        name = sanitize_mcp_name_component(item.get("name") or "")[:64]
        if not name:
            continue
        reason = item.get("reason")
        pairs.append((name, reason if reason in UNAVAILABLE_REASONS else "error"))
    return pairs


def is_composio_router_name(name: str, toolset: str | None = None) -> bool:
    """True if *name* is an MCP tool advertised by the Composio tool-router.

    By its *toolset* when known: a shortened name may not keep the prefix.
    """
    return toolset == COMPOSIO_TOOL_ROUTER_TOOLSET or name.startswith(COMPOSIO_TOOL_ROUTER_PREFIXES)


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
        # Discovery is tracked per agent because the worker shares one
        # ToolRegistry across every agent it serves.  Maps agent_id ->
        # the set of mcp__ tool names that agent may use.
        self._discovered_by_agent: dict[str, set[str]] = {}
        # Subset of the above that came from the Composio tool-router, so the
        # harness can hide a channel agent's own-platform Composio toolkit.
        self._composio_by_agent: dict[str, set[str]] = {}
        # Opt-in per-session reuse of a discovery (``cache_ttl``):
        # session_id -> (when, names, unavailable servers).
        self._session_tools: dict[UUID, tuple[float, list[str], list[tuple[str, str]]]] = {}
        # The servers a session's last discovery named unavailable, until the
        # worker takes them (``take_unavailable_servers``).
        self._unavailable_by_session: dict[UUID, list[tuple[str, str]]] = {}

    async def discover_and_register(
        self,
        org_id: UUID,
        user_id: UUID,
        session_id: UUID,
        *,
        agent_id: str,
        is_service_account: bool = False,
        cache_ttl: float = 0,
    ) -> list[str]:
        """Discover *agent_id*'s MCP tools via the proxy and register them.

        Returns the FULL set of MCP tool names available to *agent_id*
        (not just names registered on this call), so the caller can
        filter the shared ``ToolRegistry`` prompt-schema surface down to
        this agent's tools.  The proxy scopes the result to the agent's
        attached servers via the ``agent_id`` query param.

        ``is_service_account`` flags the principal so the proxy can skip
        ``users.id`` foreign keys (e.g. ``audit_log``); pass ``True``
        when ``session.user_id`` is ``None`` and we fell back to the
        session's ``service_account_id``.
        """
        # A phone call wakes the harness on every caller turn; its tool list does not change
        # mid-call, so with a ``cache_ttl`` the proxy round trip is paid once per session.
        self._unavailable_by_session.pop(session_id, None)
        if cache_ttl > 0:
            cached = self._session_tools.get(session_id)
            if cached is not None and time.monotonic() - cached[0] < cache_ttl:
                if cached[2]:
                    self._unavailable_by_session[session_id] = list(cached[2])
                return list(cached[1])

        token = create_sandbox_token(
            org_id, user_id, session_id,
            agent_id=agent_id,
            is_service_account=is_service_account,
        )
        headers = {"Authorization": f"Bearer {token}"}
        known = self._discovered_by_agent.setdefault(agent_id, set())

        resp = await self._client.post(
            "/mcp/v1/tools/list",
            headers=headers,
            params={"agent_id": agent_id},
        )
        if resp.status_code != 200:
            logger.warning(
                "MCP proxy tool discovery failed for agent %s: %d %s",
                agent_id, resp.status_code, resp.text[:200],
            )
            return sorted(known)

        data = resp.json()
        tools = data.get("tools", [])
        unavailable = _unavailable_servers(data)
        current: set[str] = set()
        composio: set[str] = set()

        for tool in tools:
            name = tool.get("name", "")
            if not name:
                continue
            current.add(name)
            # The proxy says the tool's server: a shortened name may hold
            # only part of it, and the entitlement filter reads it here.
            toolset = mcp_toolset(tool["server"]) if tool.get("server") else "mcp"
            if is_composio_router_name(name, toolset):
                composio.add(name)
            schema = ToolSchema(
                name=name,
                description=tool.get("description", ""),
                parameters=tool.get("parameters", {}),
                annotations=tool.get("annotations") or None,
            )
            # The registry is process-wide; another agent may have
            # already registered this exact tool name.  An unchanged
            # schema needs no work, but a changed one replaces it: an MCP
            # server redeployed with new arguments must reach running
            # workers, not wait for their restart.
            # ponytail: one schema per tool name per worker; per-agent
            # schemas if two agents' same-named servers ever differ.
            existing = self._registry.get(name)
            if existing is not None and existing.schema == schema and existing.toolset == toolset:
                continue
            self._registry.register(
                name=name,
                schema=schema,
                handler=self._make_proxy_handler(name),
                toolset=toolset,
                replace=True,
            )

        self._discovered_by_agent[agent_id] = current
        self._composio_by_agent[agent_id] = composio
        registered = sorted(current)
        if unavailable:
            self._unavailable_by_session[session_id] = unavailable
            logger.warning(
                "Agent %s: MCP server(s) unavailable via proxy: %s",
                agent_id, ", ".join(f"{name} ({reason})" for name, reason in unavailable),
            )
        if cache_ttl > 0:
            now = time.monotonic()
            self._session_tools = {sid: v for sid, v in self._session_tools.items() if now - v[0] < cache_ttl}
            self._session_tools[session_id] = (now, registered, unavailable)
        if registered:
            logger.info(
                "Agent %s has %d MCP tool(s) via proxy: %s",
                agent_id, len(registered), ", ".join(registered),
            )
        return registered

    def take_unavailable_servers(self, session_id: UUID) -> list[tuple[str, str]]:
        """The ``(server, reason)`` pairs *session_id*'s last discovery named
        unavailable, forgotten once taken: the worker takes them right after
        :meth:`discover_and_register`, for the session's prompt."""
        return self._unavailable_by_session.pop(session_id, [])

    def composio_tool_names_for_agent(self, agent_id: str) -> frozenset[str]:
        """Return *agent_id*'s discovered Composio tool-router tool names."""
        return frozenset(self._composio_by_agent.get(agent_id, set()))

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
            # Bind the token to this agent so the proxy can reject a
            # ``?agent_id=`` that disagrees with the minted identity.
            handler_agent_id = kwargs.get("agent_id")
            simulated = is_simulated(kwargs.get("session_config"))
            token = create_sandbox_token(
                tenant.org_id,
                principal_user_id,
                session_id,
                # One call's token: a claim it carries must not outlive it.
                expires_minutes=2 if simulated else 60,
                agent_id=str(handler_agent_id) if handler_agent_id else None,
                is_service_account=tenant.user_id is None,
                simulated=simulated,
                simulation_read=tool_name if simulated and kwargs.get("simulation_read") else None,
            )
            headers = {"Authorization": f"Bearer {token}"}

            # Forward the chat user's identity to the upstream MCP server
            # via the MCP ``_meta`` channel. The dev-mode client builds
            # the same payload in ``surogates.tools.mcp.client``, and
            # ``_is_owner_scoped`` in ``tools.builtin.session_search``
            # gates on the same ``config.ops`` stamp; keep all three in
            # sync so platform MCP servers (e.g. surogate-ops's copilot)
            # authorise proxy-mode calls the same way they authorise
            # dev-mode calls.
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
            # sessions hit before this guard.  Reuse the same id the
            # token was bound to so the query param and the JWT claim
            # always agree.
            params: dict[str, str] | None = (
                {"agent_id": str(handler_agent_id)} if handler_agent_id else None
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
