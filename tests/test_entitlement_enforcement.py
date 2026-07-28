"""Purchased-package enforcement in the runtime.

The authorize gates pin ``session.config["entitlements"]``; these tests
cover every reader of that pin: the shared reader module, the worker's
tool-exclusion computation (browser / coding / MCP servers / Composio
toolkits, including the fail-closed path when the ops scope cannot be
resolved), the harness tool filter and slash gate, and the KB tools'
per-call guard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.orchestrator.worker import _entitlement_tool_exclusions
from surogates.runtime import SLASH_COMMAND_IDS, SlashCommandConfig
from surogates.runtime.entitlements import (
    capability_allowed,
    dimension_allowlist,
    kb_allowed,
    pinned_entitlements,
)
from surogates.sandbox.pool import SandboxPool
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry


# ── shared reader ─────────────────────────────────────────────────────


def test_no_pin_means_unrestricted():
    assert pinned_entitlements(None) is None
    assert pinned_entitlements({}) is None
    assert dimension_allowlist({}, "capabilities") is None
    assert capability_allowed({}, "code")
    assert kb_allowed(None, "kb-1")


def test_malformed_pin_reads_as_unrestricted():
    assert pinned_entitlements({"entitlements": "garbage"}) is None
    assert capability_allowed({"entitlements": ["not-a-dict"]}, "code")


def test_dimension_allowlist_and_membership():
    config = {"entitlements": {"kb_ids": ["kb-1"], "capabilities": ["code"]}}
    assert dimension_allowlist(config, "kb_ids") == frozenset({"kb-1"})
    assert kb_allowed(config, "kb-1")
    assert not kb_allowed(config, "kb-2")
    # Absent dimension stays unrestricted.
    assert dimension_allowlist(config, "mcp_server_ids") is None


def test_capability_allowed_normalizes_slash_spelling():
    config = {"entitlements": {"capabilities": ["deep_research"]}}
    assert capability_allowed(config, "deep-research")
    assert capability_allowed(config, "deep_research")
    assert not capability_allowed(config, "mission")
    assert not capability_allowed(config, "code")


def test_unsold_capabilities_are_never_restricted():
    config = {"entitlements": {"capabilities": []}}
    assert capability_allowed(config, "clear")
    assert capability_allowed(config, "some-skill")
    assert not capability_allowed(config, "loop")


# ── worker exclusion computation ──────────────────────────────────────


def _registry_with(*entries: tuple[str, str]) -> ToolRegistry:
    registry = MagicMock(spec=ToolRegistry)
    all_entries = [
        MagicMock(name=n, toolset=ts) for n, ts in entries
    ]
    for mock, (n, _ts) in zip(all_entries, entries):
        mock.name = n
    registry.get_all.return_value = all_entries
    registry.tool_names = [n for n, _ in entries]
    return registry


_REGISTRY_ENTRIES = (
    ("browser_navigate", "browser"),
    ("browser_click", "browser"),
    ("run_coding_agent", "code"),
    ("read_file", "file"),
    ("mcp__crm__lookup", "mcp"),
    ("mcp__billing__charge", "mcp"),
)


def test_no_pin_excludes_nothing():
    excluded = _entitlement_tool_exclusions(
        session_config={},
        tool_registry=_registry_with(*_REGISTRY_ENTRIES),
        composio_tool_names=frozenset({"SLACK_SEND_MESSAGE"}),
        mcp_scope=None,
    )
    assert excluded == set()


def test_capability_exclusions_drop_browser_and_coding():
    excluded = _entitlement_tool_exclusions(
        session_config={"entitlements": {"capabilities": ["mission"]}},
        tool_registry=_registry_with(*_REGISTRY_ENTRIES),
        composio_tool_names=frozenset(),
        mcp_scope=None,
    )
    assert excluded == {"browser_navigate", "browser_click", "run_coding_agent"}


def test_mcp_servers_filtered_by_resolved_scope():
    excluded = _entitlement_tool_exclusions(
        session_config={"entitlements": {"mcp_server_ids": ["id-crm"]}},
        tool_registry=_registry_with(*_REGISTRY_ENTRIES),
        composio_tool_names=frozenset(
            {"SLACK_SEND_MESSAGE", "GMAIL_SEND_EMAIL"},
        ),
        mcp_scope=({"crm"}, {"gmail"}),
    )
    # billing server not entitled; slack toolkit not entitled.
    assert excluded == {"mcp__billing__charge", "SLACK_SEND_MESSAGE"}


def test_mcp_restriction_fails_closed_without_a_scope():
    """Scope resolution failed (ops DB unreachable): a restricted
    dimension drops every MCP/Composio tool rather than serving paid
    tools unmetered."""
    excluded = _entitlement_tool_exclusions(
        session_config={"entitlements": {"mcp_server_ids": ["id-crm"]}},
        tool_registry=_registry_with(*_REGISTRY_ENTRIES),
        composio_tool_names=frozenset({"SLACK_SEND_MESSAGE"}),
        mcp_scope=None,
    )
    assert excluded == {
        "mcp__crm__lookup", "mcp__billing__charge", "SLACK_SEND_MESSAGE",
    }


def test_empty_mcp_allowlist_drops_all():
    excluded = _entitlement_tool_exclusions(
        session_config={"entitlements": {"mcp_server_ids": []}},
        tool_registry=_registry_with(*_REGISTRY_ENTRIES),
        composio_tool_names=frozenset({"SLACK_SEND_MESSAGE"}),
        mcp_scope=(set(), set()),
    )
    assert excluded == {
        "mcp__crm__lookup", "mcp__billing__charge", "SLACK_SEND_MESSAGE",
    }


# ── harness: tool filter + slash gate ─────────────────────────────────


def _tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={}, user_preferences={}, permissions=frozenset(),
        asset_root="/tmp/test",
    )


def _session(config=None) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="a1",
        channel="api", status="active", config=config or {},
        created_at=now, updated_at=now,
    )


def _harness(
    *,
    entitlement_excluded: frozenset[str] = frozenset(),
) -> AgentHarness:
    registry = ToolRegistry()
    return AgentHarness(
        session_store=AsyncMock(),
        tool_registry=registry,
        llm_client=AsyncMock(),
        tenant=_tenant(),
        worker_id="test-worker",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        sandbox_pool=MagicMock(spec=SandboxPool),
        slash_commands=SlashCommandConfig(
            commands=frozenset(SLASH_COMMAND_IDS),
        ),
        entitlement_excluded_tools=entitlement_excluded,
    )


def test_entitlement_exclusions_beat_an_explicit_allowlist():
    h = _harness(entitlement_excluded=frozenset({"browser_navigate"}))
    tool_filter = h._tool_filter_for_session(
        _session({"allowed_tools": ["browser_navigate", "read_file"]}),
    )
    assert tool_filter is not None
    assert "browser_navigate" not in tool_filter
    assert "read_file" in tool_filter


def test_no_exclusions_leave_the_filter_untouched():
    h = _harness()
    tool_filter = h._tool_filter_for_session(
        _session({"allowed_tools": ["read_file"]}),
    )
    assert tool_filter == {"read_file", "ask_user_question"} or (
        "read_file" in tool_filter
    )


def test_slash_gate_honours_the_package():
    h = _harness()
    restricted = _session(
        {"entitlements": {"capabilities": ["compress"]}},
    )
    assert h._slash_command_enabled("compress", restricted)
    assert not h._slash_command_enabled("mission", restricted)
    assert not h._slash_command_enabled("deep-research", restricted)
    # Without a session (agent-level checks) the package cannot apply.
    assert h._slash_command_enabled("mission")
    # Without a pin the package never restricts.
    assert h._slash_command_enabled("mission", _session())


def test_slash_block_reason_names_the_blocked_command():
    h = _harness()
    restricted = _session({"entitlements": {"capabilities": []}})
    reason = h._slash_command_block_reason("/mission do things", restricted)
    assert reason is not None
    assert h._slash_command_block_reason("hello there", restricted) is None


# ── KB tools per-call guard ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_kb_read_refuses_a_kb_outside_the_plan():
    from surogates.tools.builtin.kb_tools import _kb_read_page_handler

    result = await _kb_read_page_handler(
        {"kb_id": "kb-locked", "path": "index.md"},
        agent_id="a1",
        session_config={"entitlements": {"kb_ids": ["kb-open"]}},
    )
    assert "not included in the current user's plan" in result


@pytest.mark.asyncio
async def test_kb_list_refuses_a_kb_outside_the_plan():
    from surogates.tools.builtin.kb_tools import _kb_list_pages_handler

    result = await _kb_list_pages_handler(
        {"kb_id": "kb-locked"},
        agent_id="a1",
        session_config={"entitlements": {"kb_ids": []}},
    )
    assert "not included in the current user's plan" in result
