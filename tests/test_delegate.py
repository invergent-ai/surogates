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
) -> None:
    """Patch resolve_agent_by_name so agent_type="x" returns the given def."""
    import surogates.harness.agent_resolver as resolver_module

    async def _stub(name, tenant, *, session_factory=None):  # noqa: ARG001
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
async def test_parent_allowlist_constrains_child_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Parent can only use read_file and write_file. The agent_type preset
    # would normally grant exec_shell, but intersection must drop it.
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
    assert "read_file" in allowed
    assert "exec_shell" not in allowed
    assert "write_file" not in allowed  # not in preset


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
