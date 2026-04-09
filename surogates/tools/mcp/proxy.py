"""MCP tool proxy -- bridges MCP servers into the ToolRegistry.

Phase 3 stub: all methods raise :class:`NotImplementedError`.
"""

from __future__ import annotations

from typing import Any

from surogates.tools.loader import MCPServerDef
from surogates.tools.mcp.client import MCPClient
from surogates.tools.registry import ToolRegistry


class MCPToolProxy:
    """Discovers tools from MCP servers and registers them into the
    :class:`ToolRegistry`.

    Each MCP tool is registered with the name prefix
    ``{server_name}_{tool_name}`` to avoid collisions across servers.

    Parameters
    ----------
    registry:
        The :class:`ToolRegistry` to register discovered tools into.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._clients: dict[str, MCPClient] = {}

    @property
    def server_names(self) -> list[str]:
        """Names of all connected MCP servers."""
        return list(self._clients.keys())

    async def add_server(self, server_def: MCPServerDef) -> None:
        """Connect to an MCP server and register its tools.

        Raises :class:`NotImplementedError` -- Phase 3.
        """
        raise NotImplementedError(
            "MCP tool proxy is not yet implemented (Phase 3)"
        )

    async def remove_server(self, server_name: str) -> None:
        """Disconnect from an MCP server and deregister its tools.

        Raises :class:`NotImplementedError` -- Phase 3.
        """
        raise NotImplementedError(
            "MCP tool proxy is not yet implemented (Phase 3)"
        )

    async def refresh_server(self, server_name: str) -> None:
        """Re-discover tools from an already connected MCP server.

        Raises :class:`NotImplementedError` -- Phase 3.
        """
        raise NotImplementedError(
            "MCP tool proxy is not yet implemented (Phase 3)"
        )

    async def shutdown(self) -> None:
        """Disconnect from all MCP servers and deregister their tools.

        Raises :class:`NotImplementedError` -- Phase 3.
        """
        raise NotImplementedError(
            "MCP tool proxy is not yet implemented (Phase 3)"
        )
