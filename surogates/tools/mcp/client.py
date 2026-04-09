"""MCP client -- connects to an MCP server via stdio or HTTP transport.

Phase 3 stub: all methods raise :class:`NotImplementedError`.
"""

from __future__ import annotations

from typing import Any

from surogates.tools.loader import MCPServerDef


class MCPClient:
    """Client for a single MCP server.

    Parameters
    ----------
    server_def:
        The :class:`MCPServerDef` describing how to connect.
    """

    def __init__(self, server_def: MCPServerDef) -> None:
        self._server_def = server_def
        self._connected = False

    @property
    def server_name(self) -> str:
        """The name of the MCP server this client is bound to."""
        return self._server_def.name

    @property
    def connected(self) -> bool:
        """Whether the client has an active connection."""
        return self._connected

    async def connect(self) -> None:
        """Establish a connection to the MCP server.

        Raises :class:`NotImplementedError` -- Phase 3.
        """
        raise NotImplementedError(
            "MCP client connections are not yet implemented (Phase 3)"
        )

    async def disconnect(self) -> None:
        """Close the connection to the MCP server.

        Raises :class:`NotImplementedError` -- Phase 3.
        """
        raise NotImplementedError(
            "MCP client connections are not yet implemented (Phase 3)"
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        """Retrieve the list of tools advertised by the MCP server.

        Returns a list of tool definitions in the MCP schema format.

        Raises :class:`NotImplementedError` -- Phase 3.
        """
        raise NotImplementedError(
            "MCP tool listing is not yet implemented (Phase 3)"
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Invoke a tool on the MCP server.

        Parameters
        ----------
        name:
            The tool name as advertised by the server.
        arguments:
            The tool arguments as a dict.

        Raises :class:`NotImplementedError` -- Phase 3.
        """
        raise NotImplementedError(
            "MCP tool execution is not yet implemented (Phase 3)"
        )

    async def __aenter__(self) -> MCPClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()
