"""Surogates tool system -- registration, routing, loading, and dispatch.

Key exports:

- :class:`ToolRegistry` -- central tool storage and dispatch.
- :class:`ToolSchema` / :class:`ToolEntry` -- data classes describing tools.
- :class:`ToolRouter` -- governance-aware dispatch via the registry.
- :class:`ToolRuntime` -- bootstrap helper that wires builtins into a registry.
- :class:`ResourceLoader` -- loads skills and MCP configs from volumes.
"""

from __future__ import annotations

from surogates.tools.coerce import coerce_tool_args
from surogates.tools.loader import MCPServerDef, ResourceLoader, SkillDef
from surogates.tools.registry import ToolEntry, ToolRegistry, ToolSchema
from surogates.tools.router import ToolLocation, ToolRouter
from surogates.tools.runtime import ToolRuntime

__all__ = [
    "MCPServerDef",
    "ResourceLoader",
    "SkillDef",
    "ToolEntry",
    "ToolLocation",
    "ToolRegistry",
    "ToolRouter",
    "ToolRuntime",
    "ToolSchema",
    "coerce_tool_args",
]
