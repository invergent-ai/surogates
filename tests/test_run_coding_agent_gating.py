"""run_coding_agent is hidden from the model when the /code capability is off.

Slash-command gating alone only blocks the human ``/code`` command; the
autonomous tool must also disappear from the model-visible tool set, otherwise
a /code-disabled agent could still invoke coding via the tool.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.runtime import SLASH_COMMAND_IDS, SlashCommandConfig
from surogates.sandbox.pool import SandboxPool
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.coding_agent import register as register_coding_tool
from surogates.tools.registry import ToolRegistry


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


def _harness(slash_commands: SlashCommandConfig) -> AgentHarness:
    registry = ToolRegistry()
    register_coding_tool(registry)
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
        slash_commands=slash_commands,
    )


def _without(*omit: str) -> SlashCommandConfig:
    return SlashCommandConfig(commands=frozenset(SLASH_COMMAND_IDS - set(omit)))


def test_coding_tool_removed_when_code_disabled():
    h = _harness(_without("code"))
    tool_filter = h._tool_filter_for_session(_session())
    assert tool_filter is not None
    assert "run_coding_agent" not in tool_filter


def test_coding_tool_present_when_code_enabled():
    h = _harness(SlashCommandConfig())  # fully permissive default
    tool_filter = h._tool_filter_for_session(_session())
    # None means "all tools" (unfiltered) — the tool is available either way.
    assert tool_filter is None or "run_coding_agent" in tool_filter


def test_disabled_code_beats_explicit_allowed_tools():
    h = _harness(_without("code"))
    session = _session({"allowed_tools": ["run_coding_agent", "read_file"]})
    tool_filter = h._tool_filter_for_session(session)
    assert tool_filter is not None
    assert "run_coding_agent" not in tool_filter
    # The rest of the explicit allow-list is untouched.
    assert "read_file" in tool_filter
