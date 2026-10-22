"""Which tools the sessions of a Keel case are offered.

A case session works on a case, not with a person in a chat.  It is offered
the case's work tools, its agent's MCP tools and the self-tools its execution
needs, and nothing that talks to people, schedules work, starts other work or
edits the agent.  Only the step's own session gets the two tools that end the
step; no other session is ever offered them, whatever its config names.

The prompt the model reads and the schemas it is sent are decided in two
places, the worker and the harness, and they must agree.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.orchestrator.worker import _filter_effective_tools
from surogates.runtime.governance import build_governance_gate, governance_profile
from surogates.sandbox.pool import SandboxPool
from surogates.session.acting_principal import ActingPrincipal
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.session.provisioning import create_agent_session, create_child_session
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.runtime import ToolRuntime
from tests.test_case_step_isolation import CONTRACT, _SessionStore
from tests.test_eval_session_seeding import _route_fixtures
from tests.test_steer_loop import _final_response, _make_loop_harness

MCP_TOOL = "mcp__crm__lookup"
STEP_TOOLS = {"complete_step", "raise_hand"}
KB_TOOLS = {"kb_list_pages", "kb_read_page", "kb_search_pages"}
# Chat, scheduling, starting other work and editing the agent: none is a case's.
NEVER_IN_A_CASE = {
    "session_search", "ask_user_question", "cron_create", "start_case",
    "skill_manage", "escalate", "start_thread",
}
# What a step keeps however narrowly its scope names its work tools.
KEPT_UNDER_ANY_SCOPE = {
    "kb_list_pages", "kb_read_page", "kb_search_pages", "skills_list", "skill_view",
    "memory", "read_file", "list_files", "search_files", "todo",
}


def _registry() -> ToolRegistry:
    """Every built-in tool, one MCP tool of the agent's, and the step tools."""
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    for name in {MCP_TOOL} | (STEP_TOOLS - registry.tool_names):
        registry.register(name, ToolSchema(name=name, description="d", parameters={}), AsyncMock())
    return registry


def _tenant(session) -> TenantContext:
    return TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/assets",
        service_account_id=session.service_account_id,
    )


def _prompt_tools(session, registry: ToolRegistry) -> set[str]:
    """The tools the worker tells the model about."""
    return _filter_effective_tools(
        tools=set(registry.tool_names),
        tenant=_tenant(session),
        session=session,
        use_api_for_harness_tools=True,
        whiteboard_enabled=True,
        programs_enabled=True,
    )


def _schema_tools(session, registry: ToolRegistry) -> set[str]:
    """The tools whose schemas the harness sends the model."""
    harness = AgentHarness(
        session_store=AsyncMock(), tool_registry=registry, llm_client=AsyncMock(),
        tenant=_tenant(session), worker_id="w", budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        sandbox_pool=MagicMock(spec=SandboxPool), vision_client=None, vision_model="",
        mcp_tool_names=frozenset({MCP_TOOL}),
    )
    return harness._tool_filter_for_session(session)


async def _sent_on_a_turn(monkeypatch, session, registry: ToolRegistry, *, prompt, metadata=None) -> set[str]:
    """The tools the model is sent on one turn of *session*, whose message carries *metadata*."""
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])
    store.resolve_acting_principal = AsyncMock(
        return_value=ActingPrincipal(user_id=session.user_id, service_account_id=session.service_account_id),
    )
    harness = _make_loop_harness(session_store=store)
    harness._tools = registry
    harness._mcp_tool_names = frozenset({MCP_TOOL})
    harness._tenant = SimpleNamespace(org_id=session.org_id, user_id=session.user_id, asset_root="/tmp/test")
    harness._prompt = prompt
    sent: list[set[str]] = []

    async def llm(**kwargs):
        sent.append({schema["function"]["name"] for schema in kwargs["create_kwargs"].get("tools", [])})
        return _final_response("Done.")

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    message = SimpleNamespace(
        id=1, type=EventType.USER_MESSAGE.value, data={"content": "What is this?", "metadata": metadata},
    )
    await harness._run_loop(
        session, [{"role": "user", "content": "What is this?"}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=[message],
    )
    return sent[0]


async def _step(store, *, tools: list[str] | None = None, scoped: bool = True):
    """A step's own session, made as the Keel link makes one, with a scope naming *tools* when *scoped*."""
    _, request, _, _ = _route_fixtures("/v1/api/sessions", service_account=True)
    return await create_agent_session(
        store=store,
        storage=request.app.state.storage,
        settings=request.app.state.settings,
        org_id=uuid4(),
        user_id=None,
        agent_id="support-bot",
        channel="api",
        config={"memory_boundary": "case:C1", "workspace_boundary": "case:C1"},
        service_account_id=uuid4(),
        case_step={**CONTRACT, "tools": tools},
        case_scope={"tools": tools, "knowledge": ["kb-uuid"]} if scoped else None,
    )


def _chat(config: dict) -> Session:
    now = datetime.now(UTC)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="support-bot", channel="web",
        status="active", config=config, created_at=now, updated_at=now,
    )


async def test_a_step_gets_the_allow_list_and_its_tools():
    registry = _registry()
    step = await _step(_SessionStore())

    prompt = _prompt_tools(step, registry)
    schema = _schema_tools(step, registry)

    wanted = STEP_TOOLS | {"read_file", "kb_read_page", MCP_TOOL}
    assert wanted <= prompt
    assert prompt.isdisjoint(NEVER_IN_A_CASE)
    checked = wanted | NEVER_IN_A_CASE
    assert schema & checked == prompt & checked


async def test_a_child_of_a_step_gets_the_case_allow_list():
    registry = _registry()
    store = _SessionStore()
    step = await _step(store)
    child = await create_child_session(store=store, parent=step, channel="delegation")

    prompt = _prompt_tools(child, registry)
    schema = _schema_tools(child, registry)

    for offered in (prompt, schema):
        assert {"read_file", "kb_read_page", MCP_TOOL} <= offered
        assert offered.isdisjoint(NEVER_IN_A_CASE | STEP_TOOLS)


async def test_a_step_narrows_work_tools_only():
    registry = _registry()
    step = await _step(_SessionStore(), tools=["write_file"])

    for offered in (_prompt_tools(step, registry), _schema_tools(step, registry)):
        assert {"write_file", MCP_TOOL} | KEPT_UNDER_ANY_SCOPE | STEP_TOOLS <= offered
        assert "terminal" not in offered
        assert "browser_navigate" not in offered


def test_an_ordinary_session_never_sees_the_step_tools():
    registry = _registry()
    chat = _chat({})
    # Even a session whose config names them.
    narrowed = _chat({"allowed_tools": ["read_file", *sorted(STEP_TOOLS)]})

    prompt = _prompt_tools(chat, registry)
    assert {"terminal", "session_search", "ask_user_question"} <= prompt
    assert prompt.isdisjoint(STEP_TOOLS)
    schema = _schema_tools(chat, registry)
    assert {"terminal", "ask_user_question"} <= schema
    assert schema.isdisjoint(STEP_TOOLS)
    assert _schema_tools(narrowed, registry) == {"read_file"}


def test_an_agent_allow_list_never_takes_the_step_tools():
    assert STEP_TOOLS <= set(governance_profile({"allowed_tools": ["read_file"]})["allowed_tools"])
    for policy in ({"allowed_tools": ["read_file"]}, {"denied_tools": sorted(STEP_TOOLS)}):
        gate = build_governance_gate(policy)
        assert all(gate.offers(name) and gate.check(name, {}).allowed for name in STEP_TOOLS), policy


@pytest.mark.parametrize("scoped", [True, False], ids=["scope-names-no-tools", "no-scope"])
async def test_a_step_without_narrowed_tools_keeps_its_agents_work_tools(scoped):
    registry = _registry()
    step = await _step(_SessionStore(), scoped=scoped)

    assert {"delegate_task", "spawn_worker", "terminal"} <= _prompt_tools(step, registry)
    # A session that coordinates no one is sent no worker tools, a case's or not.
    assert {"delegate_task", "terminal"} <= _schema_tools(step, registry)


async def test_a_knowledge_tool_dropped_for_an_agent_without_knowledge_stays_dropped(monkeypatch):
    registry = _registry()
    # Its scope keeps the knowledge tools, were there any knowledge to read.
    step = await _step(_SessionStore(), tools=["write_file"])

    # The worker drops them first when the agent has no knowledge base.
    prompt = _filter_effective_tools(
        tools=set(registry.tool_names) - KB_TOOLS, tenant=_tenant(step), session=step,
        use_api_for_harness_tools=True,
    )
    sent = await _sent_on_a_turn(monkeypatch, step, registry, prompt=SimpleNamespace(has_agents=False, has_kbs=False))

    for offered in (prompt, sent):
        assert {"read_file", "write_file"} <= offered
        assert offered.isdisjoint(KB_TOOLS)


async def test_a_board_turn_never_hands_a_case_the_board(monkeypatch):
    registry = _registry()
    board = SimpleNamespace(has_agents=False, has_whiteboard=True)
    drawn = {"whiteboard": {"mode": "sketch"}}
    step = await _step(_SessionStore())

    sent = await _sent_on_a_turn(monkeypatch, step, registry, prompt=board, metadata=drawn)

    assert "whiteboard_draw" not in sent
    assert sent <= _schema_tools(step, registry)
    # An ordinary chat's board turn is still narrowed to drawing.
    assert await _sent_on_a_turn(monkeypatch, _chat({}), registry, prompt=board, metadata=drawn) == {"whiteboard_draw"}
