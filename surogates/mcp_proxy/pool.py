"""MCP connection pool manager.

Manages ``MCPServerTask`` instances keyed by ``(org_id, user_id,
server_name)``.  Connections are created lazily on first tool-list or
tool-call request and evicted after an idle timeout.

The pool reuses the MCP client's module-level background event loop
and ``_servers`` dict, but prefixes each server name with the tenant
context to prevent collisions across organisations and users.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from surogates.audit import (
    AuditStore,
    AuditType,
    mcp_scan_event,
    rug_pull_event,
)
from surogates.governance.mcp_scanner import MCPGovernance, _fingerprint
from surogates.tools.mcp.client import (
    _MCP_AVAILABLE,
    _lock,
    _servers,
    _sanitize_error,
    discover_mcp_tools,
    sanitize_mcp_name_component,
)

logger = logging.getLogger(__name__)


def _tenant_prefix(org_id: UUID, user_id: UUID) -> str:
    """Build the server-name prefix that makes module-level entries unique."""
    return f"{org_id}_{user_id}"


def _prefixed_name(org_id: UUID, user_id: UUID, server_name: str) -> str:
    """Full tenant-scoped server name used as key in the global _servers dict."""
    return f"{_tenant_prefix(org_id, user_id)}__{server_name}"


@dataclass
class PoolEntry:
    """Tracks a tenant's MCP server set and its last-access time.

    ``governance`` is the tenant-scoped :class:`MCPGovernance` instance
    that holds this tenant's rug-pull fingerprints.  One per
    ``(org_id, user_id)`` pool entry so fingerprints never leak across
    tenants — a rug-pull in tenant A's server must not suppress a scan
    in tenant B's differently-configured server with the same name.
    """

    org_id: UUID
    user_id: UUID
    server_names: list[str]  # original (unprefixed) server names
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    # Reverse index: clean tool name -> (prefixed server key, original MCP tool name)
    tool_index: dict[str, tuple[str, str]] = field(default_factory=dict)
    sanitized_prefix: str = ""
    last_used: float = field(default_factory=time.monotonic)
    governance: MCPGovernance | None = None


class ConnectionPool:
    """Manages tenant-scoped MCP server connections.

    Parameters
    ----------
    idle_timeout:
        Seconds of inactivity before a tenant's connections are evicted.
    max_per_org:
        Maximum number of concurrent MCP connections per organisation.
    governance_enabled:
        When True, every advertised MCP tool is scanned for prompt
        injection, hidden instructions, invisible Unicode, etc. — and
        tracked for rug-pull mutations on reconnect.  Each pool entry
        gets its own :class:`MCPGovernance` instance so fingerprints
        are tenant-scoped.  Unsafe tools are filtered out of the
        advertised schema set.
    audit_store:
        Optional :class:`AuditStore` that receives
        ``policy.mcp_scan`` and ``policy.rug_pull`` entries per scanned
        tool.  Has no effect when *governance_enabled* is False.
    """

    def __init__(
        self,
        idle_timeout: int = 300,
        max_per_org: int = 50,
        *,
        governance_enabled: bool = False,
        audit_store: AuditStore | None = None,
    ) -> None:
        self._idle_timeout = idle_timeout
        self._max_per_org = max_per_org
        self._entries: dict[tuple[UUID, UUID], PoolEntry] = {}
        self._locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}
        self._eviction_task: asyncio.Task | None = None
        self._governance_enabled = governance_enabled
        self._audit_store = audit_store

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_eviction_loop(self) -> None:
        """Start the background idle-eviction loop."""
        if self._eviction_task is None or self._eviction_task.done():
            self._eviction_task = asyncio.create_task(
                self._evict_idle(), name="mcp-pool-eviction",
            )

    async def shutdown(self) -> None:
        """Shut down all connections and stop the eviction loop."""
        if self._eviction_task and not self._eviction_task.done():
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except asyncio.CancelledError:
                pass

        for entry in list(self._entries.values()):
            await self._disconnect_entry(entry)
        self._entries.clear()
        self._locks.clear()

    # ------------------------------------------------------------------
    # Cache query
    # ------------------------------------------------------------------

    def get_cached_schemas(
        self, org_id: UUID, user_id: UUID,
    ) -> list[dict[str, Any]] | None:
        """Return cached tool schemas if the tenant is connected, else None."""
        entry = self._entries.get((org_id, user_id))
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        return entry.tool_schemas

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    async def ensure_connected(
        self,
        org_id: UUID,
        user_id: UUID,
        configs: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Ensure MCP servers for this tenant are connected and return
        their tool schemas.

        Idempotent — if already connected, returns cached schemas.
        """
        key = (org_id, user_id)
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_used = time.monotonic()
                return entry.tool_schemas

            # Prefix each server name for tenant isolation.
            prefixed: dict[str, dict[str, Any]] = {}
            original_names: list[str] = []
            for name, cfg in configs.items():
                pname = _prefixed_name(org_id, user_id, name)
                prefixed[pname] = cfg
                original_names.append(name)

            from surogates.tools.registry import ToolRegistry

            temp_registry = ToolRegistry()
            registered = await asyncio.to_thread(
                discover_mcp_tools,
                servers=prefixed,
                registry=temp_registry,
            )

            schemas = temp_registry.get_schemas(names=set(registered))

            # Build clean schemas and reverse tool index.
            tenant_pfx = sanitize_mcp_name_component(
                _tenant_prefix(org_id, user_id),
            )
            clean_schemas: list[dict[str, Any]] = []
            tool_index: dict[str, tuple[str, str]] = {}

            # Per-tenant MCPGovernance — fingerprints are keyed by
            # (server, tool) within this instance, never shared with
            # other tenants even when server names collide.
            tenant_governance: MCPGovernance | None = (
                MCPGovernance() if self._governance_enabled else None
            )

            for schema in schemas:
                clean = dict(schema)
                raw_name = clean.get("name", "")

                # raw_name: mcp_{tenant_prefix}__{server}_{tool}
                # clean:    mcp_{server}_{tool}
                if raw_name.startswith(f"mcp_{tenant_pfx}__"):
                    clean_name = "mcp_" + raw_name[len(f"mcp_{tenant_pfx}__"):]
                    clean["name"] = clean_name
                else:
                    clean_name = raw_name

                # Build reverse index for O(1) tool call routing.
                # Find which prefixed server owns this tool.
                owning_server_key: str | None = None
                original_tool: str = ""
                with _lock:
                    for server_key, server in _servers.items():
                        if hasattr(server, "_registered_tool_names") and raw_name in server._registered_tool_names:
                            safe_key = sanitize_mcp_name_component(server_key)
                            prefix = f"mcp_{safe_key}_"
                            original_tool = raw_name[len(prefix):] if raw_name.startswith(prefix) else raw_name
                            owning_server_key = server_key
                            break

                # Governance scan — run before adding the tool to the
                # advertised schema set so unsafe tools never reach the
                # agent.  Skipped when governance is disabled on the pool.
                if tenant_governance is not None:
                    original_server = ""
                    if owning_server_key is not None:
                        # Strip the tenant prefix back off the server key.
                        expected_prefix = f"{_tenant_prefix(org_id, user_id)}__"
                        if owning_server_key.startswith(expected_prefix):
                            original_server = owning_server_key[len(expected_prefix):]
                        else:
                            original_server = owning_server_key

                    if not await self._scan_and_record(
                        governance=tenant_governance,
                        org_id=org_id,
                        user_id=user_id,
                        server_name=original_server,
                        tool_name=original_tool or clean_name,
                        schema=clean,
                    ):
                        continue  # unsafe or rug-pulled — exclude from advertised set

                clean_schemas.append(clean)
                if owning_server_key is not None:
                    tool_index[clean_name] = (owning_server_key, original_tool)

            entry = PoolEntry(
                org_id=org_id,
                user_id=user_id,
                server_names=original_names,
                tool_schemas=clean_schemas,
                tool_index=tool_index,
                sanitized_prefix=tenant_pfx,
                governance=tenant_governance,
            )
            self._entries[key] = entry

            logger.info(
                "Connected %d MCP server(s) for org=%s user=%s (%d tools)",
                len(original_names), org_id, user_id, len(clean_schemas),
            )
            return clean_schemas

    # ------------------------------------------------------------------
    # Governance scan helper
    # ------------------------------------------------------------------

    async def _scan_and_record(
        self,
        *,
        governance: MCPGovernance,
        org_id: UUID,
        user_id: UUID,
        server_name: str,
        tool_name: str,
        schema: dict[str, Any],
    ) -> bool:
        """Scan *schema* for safety + rug-pull; return True if tool is safe.

        *governance* is tenant-scoped (one instance per :class:`PoolEntry`)
        so fingerprints never leak between tenants.  Emits a
        ``policy.mcp_scan`` row for every scan and an additional
        ``policy.rug_pull`` row when a previously-registered tool's
        fingerprint has changed.  Unsafe or rug-pulled tools return
        ``False`` and are excluded from the advertised schema set.
        """
        # MCPGovernance expects MCP-style tool_def with ``inputSchema``;
        # schemas here are OpenAI-shaped with ``parameters``.  Adapt.
        tool_def = {
            "name": tool_name,
            "description": schema.get("description", ""),
            "inputSchema": schema.get("parameters") or {},
            "_server_name": server_name,
        }
        qualified_name = f"{server_name}.{tool_name}"

        # Rug-pull check — only meaningful when we've seen this tool
        # before within the same tenant's governance instance.
        if governance.has_fingerprint(qualified_name):
            if not governance.check_rug_pull(qualified_name, tool_def):
                if self._audit_store is not None:
                    await self._audit_store.emit(
                        org_id=org_id,
                        user_id=user_id,
                        type=AuditType.POLICY_RUG_PULL,
                        data=rug_pull_event(
                            server_name, tool_name,
                            previous_fingerprint=(
                                governance.get_fingerprint(qualified_name) or ""
                            ),
                            current_fingerprint=_fingerprint(tool_def),
                        ),
                    )
                logger.warning(
                    "Rug-pull detected for %s from server %s — tool excluded",
                    tool_name, server_name,
                )
                return False

        result = governance.scan_tool(tool_def)

        if self._audit_store is not None:
            await self._audit_store.emit(
                org_id=org_id,
                user_id=user_id,
                type=AuditType.POLICY_MCP_SCAN,
                data=mcp_scan_event(
                    server_name, tool_name,
                    safe=result.safe,
                    threats=list(result.threats),
                    severity=result.severity,
                ),
            )

        if not result.safe:
            logger.warning(
                "Unsafe MCP tool %s from server %s [%s]: %s",
                tool_name, server_name, result.severity,
                "; ".join(result.threats),
            )
            return False

        governance.register_fingerprint(qualified_name, tool_def)
        return True

    # ------------------------------------------------------------------
    # Tool calling
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        org_id: UUID,
        user_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Call an MCP tool and return the result string.

        Uses the reverse tool index for O(1) server lookup.
        """
        key = (org_id, user_id)
        entry = self._entries.get(key)
        if entry is None:
            return json.dumps({"error": "No MCP connections for this tenant."})

        entry.last_used = time.monotonic()

        # O(1) lookup via reverse index.
        routing = entry.tool_index.get(tool_name)
        if routing is None:
            return json.dumps({"error": f"Tool '{tool_name}' not found."})

        server_key, original_tool = routing

        # Read only the specific server under the lock.
        with _lock:
            server = _servers.get(server_key)

        if server is None or server.session is None:
            return json.dumps({
                "error": f"MCP server for tool '{tool_name}' is not connected.",
            })

        return await self._execute_tool_call(server, original_tool, arguments)

    @staticmethod
    async def _execute_tool_call(
        server: Any,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Execute a tool call on the MCP server session."""
        try:
            result = await server.session.call_tool(tool_name, arguments=arguments)

            if result.isError:
                error_text = ""
                for block in (result.content or []):
                    if hasattr(block, "text"):
                        error_text += block.text
                return json.dumps({
                    "error": _sanitize_error(
                        error_text or "MCP tool returned an error",
                    ),
                })

            parts: list[str] = []
            for block in (result.content or []):
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "data"):
                    parts.append(f"[binary: {getattr(block, 'mimeType', 'unknown')}]")

            output = "\n".join(parts)
            return _sanitize_error(output)

        except asyncio.TimeoutError:
            return json.dumps({
                "error": f"MCP tool call '{tool_name}' timed out.",
            })
        except Exception as exc:
            return json.dumps({
                "error": _sanitize_error(
                    f"MCP call failed: {type(exc).__name__}: {exc}",
                ),
            })

    # ------------------------------------------------------------------
    # Idle eviction
    # ------------------------------------------------------------------

    async def _disconnect_entry(self, entry: PoolEntry) -> None:
        """Remove all tenant-scoped servers from the module-level dict."""
        servers_to_shutdown = []
        with _lock:
            for name in entry.server_names:
                prefixed = _prefixed_name(entry.org_id, entry.user_id, name)
                server = _servers.pop(prefixed, None)
                if server is not None:
                    servers_to_shutdown.append(server)

        for server in servers_to_shutdown:
            try:
                await server.shutdown()
            except Exception:
                logger.debug(
                    "Error shutting down MCP server %s", server.name,
                    exc_info=True,
                )

    async def _evict_idle(self) -> None:
        """Background loop that evicts idle tenant connections."""
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return

            now = time.monotonic()
            evicted: list[tuple[UUID, UUID]] = []

            for key, entry in list(self._entries.items()):
                if now - entry.last_used > self._idle_timeout:
                    await self._disconnect_entry(entry)
                    evicted.append(key)

            for key in evicted:
                self._entries.pop(key, None)
                self._locks.pop(key, None)
                logger.info(
                    "Evicted idle MCP connections for org=%s user=%s",
                    key[0], key[1],
                )
