"""The research tools must be registered by the default ToolRuntime.

Catches the cross-module wiring step where a new builtin module gets
added to ``surogates/tools/runtime.py`` but forgotten in the
``modules`` registration list, in which case ``register_builtins``
imports the module but never calls its ``register()`` and the LLM
silently lacks the tool.
"""

from __future__ import annotations

from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime


def test_research_tools_registered_by_default_runtime() -> None:
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()

    names = set(registry.tool_names)
    assert "research_memory" in names
    assert "research_outline" in names


def test_research_tools_carry_research_toolset() -> None:
    """The renderer/event-collector code on the SDK keys on the tool
    name, but the audit + governance layers key on ``toolset``.  Pin
    the value so a future rename here surfaces as a test break rather
    than a quiet ABAC miss."""

    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()

    memory_entry = registry.get("research_memory")
    outline_entry = registry.get("research_outline")
    assert memory_entry is not None
    assert outline_entry is not None
    assert memory_entry.toolset == "research"
    assert outline_entry.toolset == "research"
