"""MCP (Model Context Protocol) integration.

This package provides the client and proxy layers for connecting to
external MCP servers, discovering their tools, and registering them
into the Surogates ToolRegistry.

The ``mcp`` Python package is an optional dependency.  When not installed,
all public entry points degrade gracefully to no-ops.
"""

from __future__ import annotations

from surogates.tools.mcp.client import (
    MCPServerTask,
    SamplingHandler,
    discover_mcp_tools,
    get_mcp_status,
    sanitize_mcp_name_component,
    set_sampling_llm_caller,
    shutdown_mcp_servers,
)
from surogates.tools.mcp.proxy import MCPToolProxy

__all__ = [
    "MCPServerTask",
    "MCPToolProxy",
    "SamplingHandler",
    "discover_mcp_tools",
    "get_mcp_status",
    "sanitize_mcp_name_component",
    "set_sampling_llm_caller",
    "shutdown_mcp_servers",
]
