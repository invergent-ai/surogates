"""The harness filters model-visible MCP schemas to this agent's set.

The worker's ToolRegistry is process-wide, so it may hold mcp__ tools
discovered for other agents.  _apply_mcp_schema_filter must drop the
foreign ones while leaving non-MCP tools untouched.
"""

from __future__ import annotations

from surogates.harness.loop import AgentHarness


class _Reg:
    def __init__(self, names):
        self.tool_names = set(names)


def _harness(registry_names, mine):
    h = AgentHarness.__new__(AgentHarness)
    h._tools = _Reg(registry_names)
    h._mcp_tool_names = frozenset(mine)
    return h


def test_foreign_mcp_tools_dropped_default_session():
    h = _harness(
        registry_names={
            "send_message", "mcp__github__list", "mcp__jira__search",
        },
        mine={"mcp__github__list"},
    )
    # Default worker session: no explicit allow-list.
    result = h._apply_mcp_schema_filter(
        {"send_message", "mcp__github__list", "mcp__jira__search"},
        explicit_allowed=False,
    )
    assert result == {"send_message", "mcp__github__list"}


def test_none_filter_materialised_and_scoped():
    h = _harness(
        registry_names={"send_message", "mcp__github__list", "mcp__jira__x"},
        mine={"mcp__github__list"},
    )
    result = h._apply_mcp_schema_filter(None, explicit_allowed=False)
    assert "mcp__jira__x" not in result
    assert "mcp__github__list" in result
    assert "send_message" in result


def test_explicit_allowlist_intersects_mcp_only():
    h = _harness(
        registry_names={"a", "mcp__github__list", "mcp__github__write"},
        mine={"mcp__github__list"},
    )
    # Admin allowed write too, but the agent never discovered it.
    result = h._apply_mcp_schema_filter(
        {"a", "mcp__github__list", "mcp__github__write"},
        explicit_allowed=True,
    )
    assert result == {"a", "mcp__github__list"}
