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
    """Tracks a tenant's MCP server set and its last-access time."""

    org_id: UUID
    user_id: UUID
    server_names: list[str]  # original (unprefixed) server names
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    # Reverse index: clean tool name -> (prefixed server key, original MCP tool name)
    tool_index: dict[str, tuple[str, str]] = field(default_factory=dict)
    sanitized_prefix: str = ""
    last_used: float = field(default_factory=time.monotonic)


class ConnectionPool:
    """Manages tenant-scoped MCP server connections.

    Parameters
    ----------
    idle_timeout:
        Seconds of inactivity before a tenant's connections are evicted.
    max_per_org:
        Maximum number of concurrent MCP connections per organisation.
    """

    def __init__(
        self,
        idle_timeout: int = 300,
        max_per_org: int = 50,
    ) -> None:
        self._idle_timeout = idle_timeout
        self._max_per_org = max_per_org
        self._entries: dict[tuple[UUID, UUID], PoolEntry] = {}
        self._locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}
        self._eviction_task: asyncio.Task | None = None

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

                clean_schemas.append(clean)

                # Build reverse index for O(1) tool call routing.
                # Find which prefixed server owns this tool.
                with _lock:
                    for server_key, server in _servers.items():
                        if hasattr(server, "_registered_tool_names") and raw_name in server._registered_tool_names:
                            safe_key = sanitize_mcp_name_component(server_key)
                            prefix = f"mcp_{safe_key}_"
                            original_tool = raw_name[len(prefix):] if raw_name.startswith(prefix) else raw_name
                            tool_index[clean_name] = (server_key, original_tool)
                            break

            entry = PoolEntry(
                org_id=org_id,
                user_id=user_id,
                server_names=original_names,
                tool_schemas=clean_schemas,
                tool_index=tool_index,
                sanitized_prefix=tenant_pfx,
            )
            self._entries[key] = entry

            logger.info(
                "Connected %d MCP server(s) for org=%s user=%s (%d tools)",
                len(original_names), org_id, user_id, len(clean_schemas),
            )
            return clean_schemas

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
