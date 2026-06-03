"""MCP (Model Context Protocol) integration.

This package provides the client layer used by the MCP proxy service to
talk to upstream MCP servers, discover their tools, and resolve calls.

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

__all__ = [
    "MCPServerTask",
    "SamplingHandler",
    "discover_mcp_tools",
    "get_mcp_status",
    "sanitize_mcp_name_component",
    "set_sampling_llm_caller",
    "shutdown_mcp_servers",
]
