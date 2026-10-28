"""Which tools the sessions of a Keel case are offered.

A case session works on a case, not with a person in a chat.  It is offered
the case's work tools, its agent's MCP tools and the self-tools its execution
needs, and nothing that talks to people, schedules work, starts other work or
edits the agent.  Only the step's own session gets the two tools that end the
step, and only a work item's own session the two that report the item; no
other session is ever offered them, whatever its config names.

A person's case agent reads its case only through Keel: it is offered the
case tools in place of the skills, the terminal, file writing, office and the
helpers, and no other session is offered the case tools.

The designer's tools work on a builder's draft: only an agent whose policy's
allow-list names them is offered them, and never in a case's session.

A draft step's agent edits the case's documents beside the person: only the
draft step's own session keeps the office tools, whatever its scope names.

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
from surogates.governance.policy import GovernanceGate
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
from tests.test_case_step_isolation import CONTRACT, ITEM_CONTRACT, _SessionStore
from tests.test_eval_session_seeding import _route_fixtures
from tests.test_harness_resilience import _make_harness
from tests.test_steer_loop import _final_response, _make_loop_harness

MCP_TOOL = "mcp__crm__lookup"
OFFICE_TOOLS = {"office_read", "office_edit"}
STEP_TOOLS = {"complete_step", "raise_hand"}
ITEM_TOOLS = {"complete_item", "return_item"}
CASE_TOOLS = {"case_read", "case_history", "case_files", "case_file_read", "case_draft_form"}
DESIGNER_TOOLS = {
    "draft_files", "draft_read", "draft_main", "draft_save_file", "draft_save_scenario", "draft_dry_run",
}
HELPER_TOOLS = {"delegate_task", "spawn_worker", "send_worker_message", "stop_worker"}
# What a person's case agent never gets: skills, the terminal, file writing,
# office, and the helpers, any of which would work in the case's workspace.
NEVER_FOR_A_CASE_AGENT = HELPER_TOOLS | {
    "skills_list", "skill_view", "skill_step", "terminal", "process", "write_file", "patch",
    "office_read", "office_edit",
}
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
    """Every built-in tool, the step tools among them, the office tools a runtime with agent editing has, and one
    MCP tool of the agent's."""
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    for name in (MCP_TOOL, *sorted(OFFICE_TOOLS)):
        registry.register(name, ToolSchema(name=name, description="d", parameters={}), AsyncMock())
    return registry


def _tenant(session) -> TenantContext:
    return TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/assets",
        service_account_id=session.service_account_id,
    )


def _prompt_tools(session, registry: ToolRegistry, *, gate: GovernanceGate | None = None) -> set[str]:
    """The tools the worker tells the model about, under the wake's governance *gate*."""
    return _filter_effective_tools(
        tools=set(registry.tool_names),
        tenant=_tenant(session),
        session=session,
        use_api_for_harness_tools=True,
        whiteboard_enabled=True,
        programs_enabled=True,
        allow_listed=gate.allow_listed(DESIGNER_TOOLS) if gate is not None else frozenset(),
    )


def _schema_tools(session, registry: ToolRegistry, *, gate: GovernanceGate | None = None) -> set[str]:
    """The tools whose schemas the harness sends the model, under the wake's governance *gate*."""
    harness = AgentHarness(
        session_store=AsyncMock(), tool_registry=registry, llm_client=AsyncMock(),
        tenant=_tenant(session), worker_id="w", budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        sandbox_pool=MagicMock(spec=SandboxPool), vision_client=None, vision_model="",
        mcp_tool_names=frozenset({MCP_TOOL}), governance_gate=gate,
    )
    return harness._tool_filter_for_session(session)


def _offered(session, registry: ToolRegistry, *, policy: dict | None) -> tuple[set[str], set[str]]:
    """The tools the worker tells the model about, and those whose schemas the harness sends it, under its
    agent's *policy*: one gate for both, as a wake builds it."""
    gate = build_governance_gate(policy)
    return _prompt_tools(session, registry, gate=gate), _schema_tools(session, registry, gate=gate)


async def _sent_on_a_turn(
    monkeypatch, session, registry: ToolRegistry, *, prompt, metadata=None, policy: dict | None = None,
) -> set[str]:
    """The tools the model is sent on one turn of *session*, whose message carries *metadata*, under its
    agent's *policy*."""
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
    harness._governance_gate = build_governance_gate(policy) if policy is not None else None
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


async def _case_session(
    store, *, tools: list[str] | None = None, scoped: bool = True, file_pane: bool = False, **contract,
):
    """A case's own session, made as the Keel link makes one: a step's (*case_step*) or a work item's
    (*case_item*), a rerun's when it also names *case_rerun*, with a scope naming *tools* when *scoped*, and a file
    pane when *file_pane*."""
    _, request, _, _ = _route_fixtures("/v1/api/sessions", service_account=True)
    config: dict = {"memory_boundary": "case:C1", "workspace_boundary": "case:C1"}
    if file_pane:
        config["file_pane"] = True
    return await create_agent_session(
        store=store,
        storage=request.app.state.storage,
        settings=request.app.state.settings,
        org_id=uuid4(),
        user_id=None,
        agent_id="support-bot",
        channel="api",
        config=config,
        service_account_id=uuid4(),
        case_scope={"tools": tools, "knowledge": ["kb-uuid"]} if scoped else None,
        **contract,
    )


async def _step(store, *, tools: list[str] | None = None, scoped: bool = True, file_pane: bool = False):
    """A step's own session, with a scope naming *tools* when *scoped*, and a file pane when *file_pane*."""
    return await _case_session(
        store, tools=tools, scoped=scoped, file_pane=file_pane, case_step={**CONTRACT, "tools": tools},
    )


async def _draft_step(store, *, tools: list[str] | None = None):
    """A draft step's own session, with the file pane the Keel link gives it, and a scope naming *tools*."""
    return await _case_session(
        store, tools=tools, file_pane=True, case_step={**CONTRACT, "mode": "draft", "tools": tools},
    )


async def _item(store, *, file_pane: bool = False):
    """A work item's own session, with a file pane when *file_pane*."""
    return await _case_session(store, file_pane=file_pane, case_item=ITEM_CONTRACT)


async def _case_agent(store, *, file_pane: bool = False):
    """A person's case agent session, made as the Keel link makes one, with a file pane when *file_pane*."""
    _, request, _, _ = _route_fixtures("/v1/api/sessions", service_account=True)
    case_id = str(uuid4())
    config: dict = {"memory_boundary": f"case:{case_id}"}
    if file_pane:
        config["file_pane"] = True
    return await create_agent_session(
        store=store,
        storage=request.app.state.storage,
        settings=request.app.state.settings,
        org_id=uuid4(),
        user_id=None,
        agent_id="support-bot",
        channel="api",
        config=config,
        service_account_id=uuid4(),
        case_agent={"case_id": case_id, "case_number": "LD-000001", "person_id": str(uuid4())},
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


async def test_only_the_items_own_session_has_the_item_tools(monkeypatch):
    registry = _registry()
    store = _SessionStore()
    item = await _item(store)

    for offered in (_prompt_tools(item, registry), _schema_tools(item, registry)):
        assert ITEM_TOOLS | {"read_file", "kb_read_page", MCP_TOOL} <= offered
        assert offered.isdisjoint(STEP_TOOLS | NEVER_IN_A_CASE)

    step = await _step(store)
    helper = await create_child_session(store=store, parent=item, channel="delegation")
    # Even a chat whose config names them.
    narrowed = _chat({"allowed_tools": ["read_file", *sorted(ITEM_TOOLS)]})
    for session in (step, helper, _chat({}), narrowed):
        for offered in (_prompt_tools(session, registry), _schema_tools(session, registry)):
            assert offered.isdisjoint(ITEM_TOOLS)

    # An agent's allow-list that names neither, or a deny-list that names both, leaves them to the item.
    plain = SimpleNamespace(has_agents=False)
    for policy in ({"allowed_tools": ["read_file"]}, {"denied_tools": sorted(ITEM_TOOLS)}):
        sent = await _sent_on_a_turn(monkeypatch, item, registry, prompt=plain, policy=policy)
        assert ITEM_TOOLS <= sent, policy
        assert all(build_governance_gate(policy).check(name, {}).allowed for name in ITEM_TOOLS), policy


async def test_a_case_agent_gets_the_case_tools_and_none_that_write_or_stage_skills():
    registry = _registry()
    agent = await _case_agent(_SessionStore())

    prompt = _prompt_tools(agent, registry)
    schema = _schema_tools(agent, registry)

    assert prompt == schema
    assert CASE_TOOLS | {"web_search", "memory", "read_file", "todo", MCP_TOOL} <= prompt
    # No helper: none could land in the case's workspace.
    assert prompt.isdisjoint(HELPER_TOOLS)
    assert prompt.isdisjoint(NEVER_FOR_A_CASE_AGENT | STEP_TOOLS | ITEM_TOOLS | NEVER_IN_A_CASE)


async def test_no_other_session_gets_the_case_tools():
    registry = _registry()
    store = _SessionStore()
    # Even a chat whose config names them.
    narrowed = _chat({"allowed_tools": ["read_file", *sorted(CASE_TOOLS)]})

    for session in (await _step(store), await _item(store), _chat({}), narrowed):
        for offered in (_prompt_tools(session, registry), _schema_tools(session, registry)):
            assert offered.isdisjoint(CASE_TOOLS), session.config


async def test_an_open_policy_offers_no_designer_tool():
    registry = _registry()
    chat = _chat({})

    for policy in (None, {"denied_tools": ["web_search"]}):
        for offered in _offered(chat, registry, policy=policy):
            assert "read_file" in offered, policy
            assert offered.isdisjoint(DESIGNER_TOOLS), policy
    # A harness without a gate, and a chat whose config names them.
    narrowed = _chat({"allowed_tools": ["read_file", *sorted(DESIGNER_TOOLS)]})
    assert _schema_tools(chat, registry).isdisjoint(DESIGNER_TOOLS)
    assert _schema_tools(narrowed, registry) == {"read_file"}


async def test_an_allow_list_naming_the_designer_tools_offers_them():
    registry = _registry()
    store = _SessionStore()
    policy = {"allowed_tools": [*sorted(DESIGNER_TOOLS), "web_search"]}

    for offered in _offered(_chat({}), registry, policy=policy):
        assert DESIGNER_TOOLS <= offered
    # Only those it names.
    for offered in _offered(_chat({}), registry, policy={"allowed_tools": ["draft_files", "draft_read"]}):
        assert offered & DESIGNER_TOOLS == {"draft_files", "draft_read"}
    # Never in a case's session.
    for session in (await _step(store), await _item(store), await _case_agent(store)):
        for offered in _offered(session, registry, policy=policy):
            assert offered.isdisjoint(DESIGNER_TOOLS), session.config


async def test_a_rerun_and_its_helpers_never_reach_the_terminal_or_the_browser():
    registry = _registry()
    store = _SessionStore()
    refused = {"terminal", "process", "browser_navigate", "browser_click"}
    rerun = await _case_session(
        store, tools=None, case_step={**CONTRACT, "tools": None}, case_rerun={"kind": "rerun"},
    )
    helper = await create_child_session(store=store, parent=rerun, channel="delegation")
    plain = await _step(store, tools=None)

    for session in (rerun, helper):
        for offered in (_prompt_tools(session, registry), _schema_tools(session, registry)):
            assert offered.isdisjoint(refused), session.id
            assert {"read_file", "kb_read_page", "web_search", MCP_TOOL} <= offered
    for offered in (_prompt_tools(rerun, registry), _schema_tools(rerun, registry)):
        assert STEP_TOOLS <= offered
    # The same step, not a rerun, works in a terminal.
    assert "terminal" in _prompt_tools(plain, registry)
    assert "terminal" in _schema_tools(plain, registry)

    # A draft rerun edits the case's documents beside the person, as a draft step does.
    draft = await _case_session(
        store, file_pane=True, case_step={**CONTRACT, "mode": "draft", "tools": None}, case_rerun={"kind": "rerun"},
    )
    for offered in (_prompt_tools(draft, registry), _schema_tools(draft, registry)):
        assert OFFICE_TOOLS | STEP_TOOLS <= offered
        assert offered.isdisjoint(refused)


async def test_a_reruns_prompt_promises_no_ssh_terminal():
    store = _SessionStore()
    plain = await _step(store)
    rerun = await _case_session(store, case_step={**CONTRACT, "tools": None}, case_rerun={"kind": "rerun"})
    harness = _make_harness(prompt_builder=SimpleNamespace(build=lambda: "BASE", folder_context=None))
    harness._ssh_targets = [{"alias": "prod", "host": "prod.invalid", "user": "deploy"}]

    # A step is told its terminal is authenticated for the agent's hosts; a rerun has no terminal.
    assert "SSH remote hosts" in await harness._build_system_prompt(plain)
    assert "SSH" not in await harness._build_system_prompt(rerun)


async def test_a_draft_step_gets_the_office_tools():
    registry = _registry()
    store = _SessionStore()
    draft = await _draft_step(store)
    narrowed = await _draft_step(store, tools=["web_search"])

    for offered in (_prompt_tools(draft, registry), _schema_tools(draft, registry)):
        assert OFFICE_TOOLS | STEP_TOOLS <= offered
    # Whatever its scope names.
    for offered in (_prompt_tools(narrowed, registry), _schema_tools(narrowed, registry)):
        assert OFFICE_TOOLS | STEP_TOOLS | {"web_search"} <= offered
        assert offered.isdisjoint({"terminal", "write_file", "browser_navigate"})


async def test_no_other_case_session_gets_the_office_tools():
    registry = _registry()
    store = _SessionStore()
    draft = await _draft_step(store)
    # A helper as delegate_task starts it, and one that declares a file pane.
    helper = await create_child_session(store=store, parent=draft, channel="delegation")
    paned_helper = await create_child_session(store=store, parent=draft, channel="api", config={"file_pane": True})

    # Each with a file pane, so only the case decides.
    others = (
        await _step(store, file_pane=True), helper, paned_helper,
        await _item(store, file_pane=True), await _case_agent(store, file_pane=True),
    )
    for session in others:
        for offered in (_prompt_tools(session, registry), _schema_tools(session, registry)):
            assert "read_file" in offered, session.config
            assert offered.isdisjoint(OFFICE_TOOLS), session.config
