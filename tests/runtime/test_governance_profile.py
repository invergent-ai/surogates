"""Per-agent governance profile: projection parsing + gate enforcement.

Covers the chain that makes the runtime-config ``governance`` blob real:

* ``governance_profile`` — normalisation of the raw blob (fail-safe on
  malformed input, disabled policies, empty profiles);
* ``build_governance_gate`` — floor composition (allow-list scope so
  MCP tools are not caught by a built-in allow-list, egress deny-all,
  frozen profile gates, unconditional workspace floor);
* ``transparency_config`` — disclosure-level clamping;
* ``execute_single_tool`` enforcement — a threaded gate denies before
  dispatch, emits ``policy.denied``, and returns the refusal to the LLM.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.tool_exec import execute_single_tool
from surogates.runtime.governance import (
    build_governance_gate,
    governance_profile,
    transparency_config,
)
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema


# ---------------------------------------------------------------------------
# governance_profile
# ---------------------------------------------------------------------------


def test_profile_none_and_empty_blob():
    assert governance_profile(None) is None
    assert governance_profile({}) is None


def test_profile_disabled_policy_is_none():
    assert governance_profile(
        {"enabled": False, "denied_tools": ["terminal"]},
    ) is None


def test_profile_no_restrictions_is_none():
    assert governance_profile(
        {"enabled": True, "allowed_tools": [], "denied_tools": []},
    ) is None


def test_profile_normalises_lists_and_drops_garbage():
    profile = governance_profile({
        "enabled": True,
        "allowed_tools": ["read_file", 42, ""],
        "denied_tools": "terminal",          # wrong type: ignored
        "egress": {"default_action": "allow", "rules": []},  # no-op egress
    })
    assert profile == {"allowed_tools": ["read_file"]}


def test_profile_keeps_deny_default_egress_without_rules():
    profile = governance_profile({
        "enabled": True,
        "egress": {"default_action": "deny", "rules": []},
    })
    assert profile == {"egress": {"default_action": "deny", "rules": []}}


def test_profile_clamps_unknown_egress_default_to_deny():
    profile = governance_profile({
        "enabled": True,
        "egress": {
            "default_action": "alow",     # typo must not widen
            "rules": [{"domain": "api.example.com", "action": "allow"}],
        },
    })
    assert profile["egress"]["default_action"] == "deny"


# ---------------------------------------------------------------------------
# build_governance_gate
# ---------------------------------------------------------------------------


def test_gate_denies_denied_tool_and_is_frozen():
    gate = build_governance_gate(
        {"enabled": True, "denied_tools": ["terminal"]},
    )
    assert gate.is_frozen
    decision = gate.check("terminal", {"command": "ls"})
    assert not decision.allowed
    assert gate.check("read_file", {"path": "notes.md"}).allowed


def test_gate_allow_list_scopes_to_builtin_tools():
    gate = build_governance_gate(
        {"enabled": True, "allowed_tools": ["read_file"]},
    )
    assert gate.check("read_file", {"path": "x"}).allowed
    assert not gate.check("terminal", {"command": "ls"}).allowed
    assert not gate.check("web_search", {"query": "q"}).allowed
    # MCP tools are governed by server attachment + entitlements, not by
    # the built-in allow-list — they must pass the role gate.
    assert gate.check("mcp__github__list_issues", {}).allowed


def test_gate_egress_deny_all_blocks_urls():
    gate = build_governance_gate({
        "enabled": True,
        "egress": {"default_action": "deny", "rules": []},
    })
    decision = gate.check("web_extract", {"url": "https://example.com"})
    assert not decision.allowed
    assert "Egress policy violation" in decision.reason


def test_gate_egress_allow_rule_passes_matching_domain():
    gate = build_governance_gate({
        "enabled": True,
        "egress": {
            "default_action": "deny",
            "rules": [{
                "domain": "api.example.com", "ports": [443],
                "protocol": "tcp", "action": "allow",
            }],
        },
    })
    assert gate.check(
        "web_extract", {"url": "https://api.example.com/v1"},
    ).allowed
    assert not gate.check(
        "web_extract", {"url": "https://evil.example.net/"},
    ).allowed


def test_gate_disabled_profile_falls_back_to_floor():
    gate = build_governance_gate(
        {"enabled": False, "denied_tools": ["terminal"]},
    )
    assert gate.check("terminal", {"command": "ls"}).allowed


def test_gate_floor_workspace_containment_survives_profile(tmp_path):
    gate = build_governance_gate(
        {"enabled": True, "denied_tools": ["terminal"]},
    )
    decision = gate.check(
        "read_file", {"path": "/etc/passwd"},
        workspace_path=str(tmp_path),
    )
    assert not decision.allowed


# ---------------------------------------------------------------------------
# transparency_config
# ---------------------------------------------------------------------------


def test_transparency_defaults_off():
    assert transparency_config(None) == {"enabled": False, "level": "none"}
    assert transparency_config({}) == {"enabled": False, "level": "none"}


def test_transparency_disabled_reports_level_none():
    assert transparency_config(
        {"transparency": {"enabled": False, "level": "full"}},
    ) == {"enabled": False, "level": "none"}


def test_transparency_unknown_level_degrades_to_basic_not_none():
    assert transparency_config(
        {"transparency": {"enabled": True, "level": "partial"}},
    ) == {"enabled": True, "level": "basic"}


def test_transparency_valid_level_passes_through():
    assert transparency_config(
        {"transparency": {"enabled": True, "level": "full"}},
    ) == {"enabled": True, "level": "full"}


# ---------------------------------------------------------------------------
# execute_single_tool enforcement
# ---------------------------------------------------------------------------


_ids = iter(range(1, 10_000))


def _make_registry(name: str = "terminal") -> tuple[ToolRegistry, AsyncMock]:
    registry = ToolRegistry()
    handler = AsyncMock(return_value='{"status": "ok"}')
    registry.register(
        name,
        ToolSchema(
            name=name,
            description="test tool",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        ),
        handler=handler,
    )
    return registry, handler


def _make_session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), config={}, agent_id="test-agent", model="gpt-4o",
    )


def _make_store() -> AsyncMock:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=lambda *a, **k: next(_ids))
    store.advance_harness_cursor = AsyncMock()
    return store


@pytest.mark.asyncio
async def test_threaded_gate_denies_before_dispatch():
    registry, handler = _make_registry("terminal")
    store = _make_store()
    gate = build_governance_gate(
        {"enabled": True, "denied_tools": ["terminal"]},
    )

    result = await execute_single_tool(
        {
            "id": "tc_1",
            "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
        },
        session=_make_session(),
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
        governance_gate=gate,
    )

    handler.assert_not_awaited()
    denied = [
        c for c in store.emit_event.call_args_list
        if c.args[1] is EventType.POLICY_DENIED
    ]
    assert denied, "policy.denied was never emitted"
    assert denied[0].args[2]["tool"] == "terminal"
    payload = json.loads(result["content"])
    assert "Blocked" in payload["error"]


@pytest.mark.asyncio
async def test_threaded_gate_allows_and_dispatches():
    registry, handler = _make_registry("terminal")
    store = _make_store()
    gate = build_governance_gate(
        {"enabled": True, "denied_tools": ["web_search"]},
    )

    result = await execute_single_tool(
        {
            "id": "tc_1",
            "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
        },
        session=_make_session(),
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
        governance_gate=gate,
    )

    handler.assert_awaited()
    assert json.loads(result["content"])["status"] == "ok"


@pytest.mark.asyncio
async def test_no_gate_preserves_open_behaviour():
    registry, handler = _make_registry("terminal")
    store = _make_store()

    await execute_single_tool(
        {
            "id": "tc_1",
            "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
        },
        session=_make_session(),
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
    )

    handler.assert_awaited()
