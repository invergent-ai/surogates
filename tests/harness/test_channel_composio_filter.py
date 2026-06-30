"""A Slack channel session hides Composio's SLACK toolkit from the tool set.

Composio's Slack tools use a different connection/identity than the channel's
bot, so a Slack channel agent must be steered to the native channel I/O. Other
Composio toolkits, native tools, and non-channel sessions are untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop import AgentHarness, _mcp_tool_component


class _Reg:
    def __init__(self, names):
        self.tool_names = set(names)


def _harness(registry_names, mcp, composio):
    h = AgentHarness.__new__(AgentHarness)
    h._tools = _Reg(registry_names)
    h._mcp_tool_names = frozenset(mcp)
    h._composio_tool_names = frozenset(composio)
    return h


def _session(channel, config=None):
    return SimpleNamespace(channel=channel, config=config or {})


_SLACK = "mcp__tool_router__SLACK_SEND_MESSAGE"
_SLACK2 = "mcp__tool_router__SLACK_FETCH_FILE"
_GMAIL = "mcp__tool_router__GMAIL_SEND_EMAIL"
_REG = {_SLACK, _SLACK2, _GMAIL, "read_file", "fetch_channel_file"}
_COMPOSIO = {_SLACK, _SLACK2, _GMAIL}


def test_mcp_tool_component_takes_last_segment():
    assert _mcp_tool_component(_SLACK) == "SLACK_SEND_MESSAGE"
    assert _mcp_tool_component("read_file") == "read_file"


def test_slack_session_drops_composio_slack_only():
    h = _harness(_REG, mcp=_COMPOSIO, composio=_COMPOSIO)
    result = h._drop_native_channel_composio_tools(set(_REG), _session("slack"))
    assert _SLACK not in result and _SLACK2 not in result
    assert _GMAIL in result
    assert {"read_file", "fetch_channel_file"} <= result


def test_non_channel_session_keeps_composio_slack():
    h = _harness(_REG, mcp=_COMPOSIO, composio=_COMPOSIO)
    result = h._drop_native_channel_composio_tools(set(_REG), _session("api"))
    assert _SLACK in result and _SLACK2 in result


def test_unmapped_channel_is_noop():
    h = _harness(_REG, mcp=_COMPOSIO, composio=_COMPOSIO)
    tf = set(_REG)
    assert h._drop_native_channel_composio_tools(tf, _session("telegram")) == tf


def test_no_composio_is_noop_even_for_slack():
    h = _harness(_REG, mcp=_COMPOSIO, composio=set())
    tf = set(_REG)
    assert h._drop_native_channel_composio_tools(tf, _session("slack")) == tf


def test_none_filter_materialises_and_drops_for_slack():
    h = _harness(_REG, mcp=_COMPOSIO, composio=_COMPOSIO)
    result = h._drop_native_channel_composio_tools(None, _session("slack"))
    assert _SLACK not in result and _GMAIL in result and "read_file" in result


def test_none_filter_preserved_when_nothing_dropped():
    # No Composio Slack tools to drop → None passes through (all-tools fast path).
    h = _harness(_REG, mcp={_GMAIL}, composio={_GMAIL})
    assert h._drop_native_channel_composio_tools(None, _session("slack")) is None


def test_native_slack_named_tool_not_caught():
    # A native (non-Composio) tool whose component starts with SLACK_ is safe
    # because the drop set is intersected with _composio_tool_names only.
    reg = _REG | {"mcp__other__SLACK_thing"}
    h = _harness(reg, mcp=_COMPOSIO | {"mcp__other__SLACK_thing"}, composio=_COMPOSIO)
    result = h._drop_native_channel_composio_tools(set(reg), _session("slack"))
    assert "mcp__other__SLACK_thing" in result


def test_ordering_drop_survives_apply_mcp_schema_filter():
    # The integration concern: _apply_mcp_schema_filter re-adds the agent's MCP
    # tools (incl. Composio SLACK), so the drop must run AFTER it.
    # A foreign MCP tool is needed so _apply_mcp_schema_filter materialises the
    # filter (it short-circuits to None when there are no foreign tools).
    _FOREIGN = "mcp__other_router__SOME_TOOL"
    reg = _REG | {_FOREIGN}
    h = _harness(reg, mcp=_COMPOSIO, composio=_COMPOSIO)
    after_mcp = h._apply_mcp_schema_filter(None, explicit_allowed=False)
    assert _SLACK in after_mcp  # re-added by the MCP filter
    assert _FOREIGN not in after_mcp  # foreign tool excluded
    final = h._drop_native_channel_composio_tools(after_mcp, _session("slack"))
    assert _SLACK not in final and _SLACK2 not in final
    assert _GMAIL in final
