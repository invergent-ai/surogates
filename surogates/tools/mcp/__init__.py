"""MCP (Model Context Protocol) integration -- Phase 3.

This package provides the client and proxy layers for connecting to
MCP servers.  The interfaces are defined here so that the
:class:`~surogates.tools.router.ToolRouter` can reference them, but
the implementations raise :class:`NotImplementedError` until Phase 3.
"""

from __future__ import annotations

from surogates.tools.mcp.client import MCPClient
from surogates.tools.mcp.proxy import MCPToolProxy

__all__ = ["MCPClient", "MCPToolProxy"]
