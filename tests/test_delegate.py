"""Tests for the ``delegate_task`` built-in tool.

Covers: backwards-compatible single delegation, batch fan-out, depth
limit, role-based tool stripping, session tracing, and stale detection.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

import surogates.session.provisioning as provisioning_module
import surogates.tools.builtin.delegate as delegate_module
from surogates.harness.budget import IterationBudget
from surogates.session.events import EventType
from surogates.session.models import Event, Session
from surogates.tools.builtin.delegate import _delegate_handler


# ─── fakes ─────────────────────────────────────────────────────────────


def _parent_session(
    *,
    delegation_depth: int = 0,
    allowed_tools: list[str] | None = None,
    excluded_tools: list[str] | None = None,
    agent_type: str | None = None,
) -> Session:
    now = datetime.now(timezone.utc)
    config: dict[str, Any] = {
        "storage_bucket": "b",
        "workspace_path": "w",
        "supports_vision": False,
    }
    if delegation_depth:
        config["delegation_depth"] = delegation_depth
    if allowed_tools is not None:
        config["allowed_tools"] = allowed_tools
    if excluded_tools is not None:
        config["excluded_tools"] = excluded_tools
    if agent_type is not None:
        config["agent_type"] = agent_type
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-a",
        channel="web",
        status="active",
        config=config,
        created_at=now,
        updated_at=now,
    )


def _event(session_id: UUID, type_: EventType, data: dict[str, Any]) -> Event:
    return Event(
        session_id=session_id,
        type=type_.value,
        data=data,
        created_at=datetime.now(timezone.utc),
    )


class FakeStore:
    """In-memory session store. Records emitted events and serves a
    pre-seeded child event log."""

    def __init__(
        self,
        parent: Session,
        *,
        child_events: list[Event] | None = None,
    ) -> None:
        self._parent = parent
        self._child_events: list[Event] = list(child_events or [])
        self._child_id: UUID | None = None
        self.parent_emitted: list[tuple[EventType, dict[str, Any]]] = []
        self.child_emitted: list[tuple[EventType, dict[str, Any]]] = []

    def set_child_id(self, child_id: UUID) -> None:
        self._child_id = child_id
        for ev in self._child_events:
            ev.session_id = child_id

    async def get_session(self, session_id: UUID) -> Session:
        if session_id == self._parent.id:
            return self._parent
        raise KeyError(session_id)

    async def emit_event(
        self,
        session_id: UUID,
        event_type: EventType,
        data: dict[str, Any],
    ) -> int:
        if session_id == self._parent.id:
            self.parent_emitted.append((event_type, data))
        else:
            self.child_emitted.append((event_type, data))
        return len(self.parent_emitted) + len(self.child_emitted)

    async def get_events(self, session_id: UUID) -> list[Event]:
        return list(self._child_events)

    async def update_session_config_key(
        self, session_id: UUID, key: str, value: Any,
    ) -> None:
        # Mirrors SessionStore: mutate the (in-memory) session config.
        if session_id == self._parent.id:
            config = dict(self._parent.config or {})
            config[key] = value
            self._parent.config = config


class _StubAgentDef:
    """Mimics an AgentDef enough for the delegate handler's reads."""

    def __init__(
        self,
        *,
        tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        model: str | None = None,
        max_iterations: int | None = None,
        policy_profile: str | None = None,
    ) -> None:
        self.tools = tools
        self.disallowed_tools = disallowed_tools
        self.model = model
        self.max_iterations = max_iterations
        self.policy_profile = policy_profile


def _install_stub_agent_resolver(
    monkeypatch: pytest.MonkeyPatch, *, agent_def: _StubAgentDef,
    captured_kwargs: dict[str, Any] | None = None,
) -> None:
    """Patch resolve_agent_by_name so agent_type="x" returns the given def.

    When *captured_kwargs* is provided, the resolver call's kwargs are
    written into it so tests can assert that the harness threads the
    bundle through to the resolver -- without that, ``delegate_task``
    cannot find sub-agents that only exist in the per-agent Hub bundle
    (the deep-research workflow regression that prompted this thread).
    """
    import surogates.harness.agent_resolver as resolver_module

    async def _stub(name, tenant, *, session_factory=None, bundle=None):  # noqa: ARG001
        if captured_kwargs is not None:
            captured_kwargs["bundle"] = bundle
            captured_kwargs["session_factory"] = session_factory
        return agent_def

    monkeypatch.setattr(resolver_module, "resolve_agent_by_name", _stub)


def _install_stub_child_session(monkeypatch, store: FakeStore) -> dict[str, Any]:
    """Patch create_child_session to return a fake child session and
    return a captured-args dict so tests can assert child config."""
    captured: dict[str, Any] = {}

    async def _stub(*, store, parent, channel, model, config, **_kwargs):
        captured["config"] = dict(config or {})
        captured["model"] = model
        child_id = uuid4()
        store.set_child_id(child_id)
        return Session(
            id=child_id,
            user_id=parent.user_id,
            org_id=parent.org_id,
            agent_id=parent.agent_id,
            channel=channel,
            status="active",
            model=model or parent.model,
            config={**(parent.config or {}), **(config or {})},
            parent_id=parent.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(provisioning_module, "create_child_session", _stub)
    return captured


def _complete_response_events(text: str) -> list[Event]:
    """Minimal child event log: LLM_RESPONSE then SESSION_COMPLETE."""
    placeholder = uuid4()
    return [
        _event(placeholder, EventType.LLM_RESPONSE, {
            "message": {"role": "assistant", "content": text},
        }),
        _event(placeholder, EventType.SESSION_COMPLETE, {}),
    ]


# ─── single-task path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_goal_returns_child_response_and_emits_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("done"))
    _install_stub_child_session(monkeypatch, store)

    result = await _delegate_handler(
        {"goal": "Fix tests"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    assert "done" in result
    parent_types = [t for t, _ in store.parent_emitted]
    assert EventType.DELEGATION_START in parent_types
    assert EventType.DELEGATION_COMPLETE in parent_types
    start_data = next(d for t, d in store.parent_emitted if t == EventType.DELEGATION_START)
    assert start_data["goal"] == "Fix tests"
    assert start_data["role"] == "leaf"
    assert start_data["depth"] == 1


# ─── validation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_goal_and_goals_returns_error() -> None:
    parent = _parent_session()
    store = FakeStore(parent)

    result = await _delegate_handler(
        {},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    assert "either `goal` or `goals` is required" in result
    assert store.parent_emitted == []


@pytest.mark.asyncio
async def test_goal_and_goals_both_set_returns_error() -> None:
    parent = _parent_session()
    store = FakeStore(parent)

    result = await _delegate_handler(
        {"goal": "x", "goals": [{"goal": "y"}]},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    assert "provide either `goal` or `goals`" in result


# ─── batch fan-out ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_goals_returns_combined_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    _install_stub_child_session(monkeypatch, store)

    result = await _delegate_handler(
        {"goals": [{"goal": "task A"}, {"goal": "task B"}]},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=20),
    )

    parsed = json.loads(result)
    assert len(parsed) == 2
    assert {p["goal"] for p in parsed} == {"task A", "task B"}
    # One DELEGATION_START + DELEGATION_COMPLETE per child.
    types = [t for t, _ in store.parent_emitted]
    assert types.count(EventType.DELEGATION_START) == 2
    assert types.count(EventType.DELEGATION_COMPLETE) == 2


# ─── depth limit ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_depth_limit_rejects_grandchild_delegation() -> None:
    # Parent is itself a depth-2 child — calling delegate_task should
    # refuse to spawn a third level.
    parent = _parent_session(delegation_depth=2)
    store = FakeStore(parent)

    result = await _delegate_handler(
        {"goal": "deep"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    assert "depth limit reached" in result
    # No child session events should have been emitted.
    assert store.parent_emitted == []


@pytest.mark.asyncio
async def test_child_inherits_incremented_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session(delegation_depth=1)
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)

    await _delegate_handler(
        {"goal": "go"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    assert captured["config"]["delegation_depth"] == 2


# ─── role-based tool stripping ────────────────────────────────────────


@pytest.mark.asyncio
async def test_leaf_role_excludes_delegate_task_from_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)

    await _delegate_handler(
        {"goal": "x", "role": "leaf"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    excluded = captured["config"].get("excluded_tools", [])
    assert "delegate_task" in excluded


@pytest.mark.asyncio
async def test_orchestrator_role_does_not_exclude_delegate_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)

    await _delegate_handler(
        {"goal": "x", "role": "orchestrator"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    excluded = captured["config"].get("excluded_tools", [])
    assert "delegate_task" not in excluded


# ─── trace ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completion_emits_trace_in_event_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    placeholder = uuid4()
    child_events = [
        _event(placeholder, EventType.TOOL_CALL, {
            "name": "read_file",
            "tool_call_id": "c1",
            "arguments": {"path": "src/foo.py"},
        }),
        _event(placeholder, EventType.TOOL_RESULT, {
            "tool_call_id": "c1", "error": False,
        }),
        _event(placeholder, EventType.TOOL_CALL, {
            "name": "grep",
            "tool_call_id": "c2",
            "arguments": {"pattern": "foo"},
        }),
        _event(placeholder, EventType.TOOL_RESULT, {
            "tool_call_id": "c2", "error": True,
        }),
        _event(placeholder, EventType.LLM_RESPONSE, {
            "message": {"role": "assistant", "content": "all done"},
        }),
        _event(placeholder, EventType.SESSION_COMPLETE, {}),
    ]
    store = FakeStore(parent, child_events=child_events)
    _install_stub_child_session(monkeypatch, store)

    result = await _delegate_handler(
        {"goal": "x"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    assert "all done" in result
    assert "delegation trace" in result
    assert "read_file" in result
    # The grep call failed; summary marks failures with "!".
    assert "grep!" in result

    complete_data = next(
        d for t, d in store.parent_emitted if t == EventType.DELEGATION_COMPLETE
    )
    assert complete_data["tool_call_count"] == 2
    names = [entry["name"] for entry in complete_data["trace"]]
    assert names == ["read_file", "grep"]
    oks = [entry["ok"] for entry in complete_data["trace"]]
    assert oks == [True, False]


# ─── stale detection ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_detection_emits_event_on_idle_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    # Child has one initial event then never progresses; never completes
    # within the test timeout.
    placeholder = uuid4()
    stuck = [_event(placeholder, EventType.USER_MESSAGE, {"content": "go"})]
    store = FakeStore(parent, child_events=stuck)
    _install_stub_child_session(monkeypatch, store)

    # Speed up the poll: tiny interval, sub-second stale threshold, hard
    # timeout just past it so the loop exits cleanly with "timed out".
    monkeypatch.setattr(delegate_module, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(delegate_module, "_IDLE_STALE_THRESHOLD_SECONDS", 0.05)
    monkeypatch.setattr(delegate_module, "_DELEGATION_TIMEOUT_SECONDS", 0.5)

    result = await asyncio.wait_for(
        _delegate_handler(
            {"goal": "x"},
            session_store=store,
            redis=None,
            tenant=object(),
            session_id=str(parent.id),
            budget=IterationBudget(max_total=10),
        ),
        timeout=3.0,
    )

    assert "timed out" in result.lower()
    types = [t for t, _ in store.parent_emitted]
    assert EventType.DELEGATION_STALE in types
    stale_data = next(d for t, d in store.parent_emitted if t == EventType.DELEGATION_STALE)
    assert stale_data["in_tool"] is False
    assert EventType.DELEGATION_FAILED in types


# ─── toolset intersection ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_def_tools_are_authoritative_over_parent_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the child has an admin-defined agent_def, its ``tools`` list
    is authoritative -- the parent's runtime allowlist no longer
    intersects it away.  Otherwise admin-defined sub-agents silently
    lose tools they explicitly require (e.g. the research-writer's
    ``create_artifact`` getting stripped because the planner -- a
    peer sub-agent -- doesn't have it).  Platform-level denials still
    propagate via excluded_tools and _DELEGATION_ALWAYS_BLOCKED_TOOLS;
    the carve-out is scoped to admin-authored agent_def tool lists.
    """
    # Parent has a narrow allowlist.  The engineer agent_def wants a
    # different tool set; under the old intersection logic that would
    # get whittled down.  Now the agent_def wins.
    parent = _parent_session(allowed_tools=["read_file", "write_file"])
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)
    _install_stub_agent_resolver(
        monkeypatch,
        agent_def=_StubAgentDef(tools=["read_file", "exec_shell"]),
    )

    await _delegate_handler(
        {"goal": "x", "agent_type": "engineer"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    allowed = captured["config"]["allowed_tools"]
    # Child receives exactly what the agent_def specifies.
    assert "read_file" in allowed
    assert "exec_shell" in allowed, (
        "exec_shell is required by the engineer agent_def; "
        "intersection with parent.allowed_tools must not strip it"
    )
    # write_file is in the parent's allowlist but NOT in the
    # agent_def's tools -- the agent_def is authoritative, so the
    # child does not inherit it.
    assert "write_file" not in allowed


@pytest.mark.asyncio
async def test_orchestrator_agent_type_keeps_delegate_task_at_default_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a sub-agent whose AGENT.md lists ``delegate_task`` in
    its tools is an orchestrator by definition (e.g. ``deep-research``,
    which uses delegate_task to hand off to ``research-writer``).  The
    default role on ``delegate_task`` is ``leaf``, and the leaf-role
    strip used to unconditionally remove ``delegate_task`` from the
    child's allowlist -- which left a planner literally unable to spawn
    its writer and silently turned the workflow into a no-op.  The fix
    treats the agent_def's tool list as authoritative: if it advertises
    delegate_task, leaf-role strip is skipped.
    """
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)
    _install_stub_agent_resolver(
        monkeypatch,
        agent_def=_StubAgentDef(
            tools=[
                "web_search", "web_extract", "research_memory",
                "research_outline", "delegate_task", "ask_user_question",
            ],
        ),
    )

    await _delegate_handler(
        # Default role; the LLM dispatch is the realistic case (no
        # ``role`` kwarg sent by the model).
        {"goal": "research X", "agent_type": "deep-research"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    allowed = captured["config"]["allowed_tools"]
    assert "delegate_task" in allowed, (
        "deep-research's AGENT.md lists delegate_task; leaf-strip "
        "must NOT remove it (it's the hand-off mechanism)"
    )


@pytest.mark.asyncio
async def test_leaf_role_still_strips_delegate_when_agent_def_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-test for the orchestrator carve-out: a leaf agent_def
    that does NOT advertise delegate_task still has it stripped.  Keeps
    the original guardrail intact for regular leaf workers."""
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)
    _install_stub_agent_resolver(
        monkeypatch,
        agent_def=_StubAgentDef(tools=["read_file", "write_file"]),
    )

    await _delegate_handler(
        {"goal": "x", "agent_type": "engineer"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    allowed = captured["config"]["allowed_tools"]
    assert "delegate_task" not in allowed, (
        "engineer agent_def doesn't list delegate_task; leaf-role "
        "strip should still remove it from the child"
    )


@pytest.mark.asyncio
async def test_delegate_forwards_bundle_to_agent_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ``delegate_task`` with ``agent_type=<name>`` resolves
    the sub-agent through the tenant catalog *plus the per-agent Hub
    bundle*; the deep-research planner and writer only exist in the
    bundle, so without the bundle being threaded through the resolver
    returns ``None`` and the LLM sees ``Unknown or disabled agent_type``.
    """
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    _install_stub_child_session(monkeypatch, store)
    captured: dict[str, Any] = {}
    _install_stub_agent_resolver(
        monkeypatch,
        agent_def=_StubAgentDef(),
        captured_kwargs=captured,
    )

    sentinel_bundle = object()
    await _delegate_handler(
        {"goal": "x", "agent_type": "deep-research"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
        bundle=sentinel_bundle,
    )

    assert captured.get("bundle") is sentinel_bundle


@pytest.mark.asyncio
async def test_deep_research_cannot_delegate_to_deep_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (c): a planner already running as ``deep-research``
    must not be able to spawn another ``deep-research`` child.  Even
    within the depth cap this multiplies work and burns the timeout on
    every node -- the failure mode that produced the 4-planner runaway
    fleet we just untangled.
    """
    parent = _parent_session(agent_type="deep-research")
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    # Resolver is patched so a *resolution* miss can't be the reason for
    # the rejection -- the guard must trigger BEFORE the child is spawned.
    _install_stub_agent_resolver(
        monkeypatch, agent_def=_StubAgentDef(),
    )
    # If the guard is missing the handler will try to create a child;
    # patching create_child_session keeps that path inert if reached.
    captured_create = _install_stub_child_session(monkeypatch, store)

    result_json = await _delegate_handler(
        {"goal": "more research", "agent_type": "deep-research"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    parsed = json.loads(result_json)
    assert "error" in parsed
    assert "cannot delegate to itself" in parsed["error"]
    # No child config was captured -- the guard fired before spawn.
    assert "config" not in captured_create


@pytest.mark.asyncio
async def test_batch_goals_rejects_deep_research_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (d): a single ``delegate_task`` with ``goals=[...]``
    targeting ``agent_type=deep-research`` must be rejected.  One call
    producing three concurrent deep-research planners is exactly the
    fan-out pattern that turned one user prompt into a runaway fleet.
    """
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    _install_stub_agent_resolver(
        monkeypatch, agent_def=_StubAgentDef(),
    )
    captured_create = _install_stub_child_session(monkeypatch, store)

    result_json = await _delegate_handler(
        {
            "goals": [
                {"goal": "topic A", "agent_type": "deep-research"},
                {"goal": "topic B", "agent_type": "deep-research"},
                {"goal": "topic C", "agent_type": "deep-research"},
            ],
        },
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    parsed = json.loads(result_json)
    assert "error" in parsed
    assert "deep-research" in parsed["error"]
    assert "one at a time" in parsed["error"]
    # Counter-check: a single-goal call with the same agent_type must
    # still go through -- the guard targets fan-out, not the agent type
    # in isolation.
    captured_create.clear()
    await _delegate_handler(
        {"goal": "topic A", "agent_type": "deep-research"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )
    assert "config" in captured_create


@pytest.mark.asyncio
async def test_child_inherits_parent_allowlist_when_no_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session(allowed_tools=["read_file", "write_file"])
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)

    await _delegate_handler(
        {"goal": "x"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    allowed = captured["config"]["allowed_tools"]
    assert set(allowed) == {"read_file", "write_file"}


@pytest.mark.asyncio
async def test_child_inherits_parent_excluded_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session(excluded_tools=["dangerous_tool"])
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)

    await _delegate_handler(
        {"goal": "x"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    excluded = captured["config"].get("excluded_tools", [])
    assert "dangerous_tool" in excluded


# ─── hardcoded blocklist ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blocklist_filters_ask_user_question_from_preset_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)
    _install_stub_agent_resolver(
        monkeypatch,
        agent_def=_StubAgentDef(tools=["read_file", "ask_user_question", "spawn_worker"]),
    )

    await _delegate_handler(
        {"goal": "x", "agent_type": "engineer"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    allowed = captured["config"]["allowed_tools"]
    assert "read_file" in allowed
    assert "ask_user_question" not in allowed
    assert "spawn_worker" not in allowed


# ─── file-change awareness ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_changes_surface_in_event_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    placeholder = uuid4()
    child_events = [
        _event(placeholder, EventType.TOOL_CALL, {
            "name": "read_file",
            "tool_call_id": "r1",
            "arguments": {"path": "src/a.py"},
        }),
        _event(placeholder, EventType.TOOL_RESULT, {
            "tool_call_id": "r1", "error": False,
        }),
        _event(placeholder, EventType.TOOL_CALL, {
            "name": "write_file",
            "tool_call_id": "w1",
            "arguments": {"path": "src/a.py", "content": "new"},
        }),
        _event(placeholder, EventType.TOOL_RESULT, {
            "tool_call_id": "w1", "error": False,
        }),
        _event(placeholder, EventType.TOOL_CALL, {
            "name": "patch",
            "tool_call_id": "p1",
            "arguments": {"path": "src/b.py", "mode": "replace"},
        }),
        _event(placeholder, EventType.TOOL_RESULT, {
            "tool_call_id": "p1", "error": False,
        }),
        _event(placeholder, EventType.LLM_RESPONSE, {
            "message": {"role": "assistant", "content": "done"},
        }),
        _event(placeholder, EventType.SESSION_COMPLETE, {}),
    ]
    store = FakeStore(parent, child_events=child_events)
    _install_stub_child_session(monkeypatch, store)

    result = await _delegate_handler(
        {"goal": "x"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    assert "files modified: src/a.py, src/b.py" in result
    assert "files read: src/a.py" in result

    complete_data = next(
        d for t, d in store.parent_emitted if t == EventType.DELEGATION_COMPLETE
    )
    assert complete_data["files_written"] == ["src/a.py", "src/b.py"]
    assert complete_data["files_read"] == ["src/a.py"]


@pytest.mark.asyncio
async def test_file_changes_deduplicate_repeat_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    placeholder = uuid4()
    child_events = [
        _event(placeholder, EventType.TOOL_CALL, {
            "name": "write_file",
            "tool_call_id": "w1",
            "arguments": {"path": "src/foo.py", "content": "v1"},
        }),
        _event(placeholder, EventType.TOOL_RESULT, {
            "tool_call_id": "w1", "error": False,
        }),
        _event(placeholder, EventType.TOOL_CALL, {
            "name": "write_file",
            "tool_call_id": "w2",
            "arguments": {"path": "src/foo.py", "content": "v2"},
        }),
        _event(placeholder, EventType.TOOL_RESULT, {
            "tool_call_id": "w2", "error": False,
        }),
        _event(placeholder, EventType.LLM_RESPONSE, {
            "message": {"role": "assistant", "content": "ok"},
        }),
        _event(placeholder, EventType.SESSION_COMPLETE, {}),
    ]
    store = FakeStore(parent, child_events=child_events)
    _install_stub_child_session(monkeypatch, store)

    await _delegate_handler(
        {"goal": "x"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    complete_data = next(
        d for t, d in store.parent_emitted if t == EventType.DELEGATION_COMPLETE
    )
    assert complete_data["files_written"] == ["src/foo.py"]


@pytest.mark.asyncio
async def test_blocklist_appears_in_excluded_tools_when_no_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_session()
    store = FakeStore(parent, child_events=_complete_response_events("ok"))
    captured = _install_stub_child_session(monkeypatch, store)

    await _delegate_handler(
        {"goal": "x"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    excluded = captured["config"].get("excluded_tools", [])
    assert "ask_user_question" in excluded
    assert "spawn_worker" in excluded
    assert "send_worker_message" in excluded
    assert "stop_worker" in excluded
