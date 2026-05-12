from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.outcomes import parse_outcome_evaluation, start_outcome
from surogates.harness.prompt import PromptBuilder
from surogates.session.events import EventType
from surogates.session.models import Session, SessionLease
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry


def _session(config: dict[str, Any] | None = None) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-a",
        channel="web",
        status="active",
        config=config or {},
        created_at=now,
        updated_at=now,
    )


def _lease(session_id: UUID) -> SessionLease:
    return SessionLease(
        session_id=session_id,
        owner_id="worker-a",
        lease_token=uuid4(),
        expires_at=datetime.now(timezone.utc),
    )


class FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, EventType, dict[str, Any]]] = []
        self.config_updates: list[tuple[UUID, str, dict[str, Any]]] = []
        self.config_clears: list[tuple[UUID, str]] = []
        self.synthetic_messages: list[tuple[UUID, str, str, dict[str, Any] | None]] = []
        self.cursor_advances: list[dict[str, Any]] = []
        self.next_event_id = 1

    async def emit_event(
        self,
        session_id: UUID,
        event_type: EventType,
        data: dict[str, Any],
    ) -> int:
        self.events.append((session_id, event_type, data))
        event_id = self.next_event_id
        self.next_event_id += 1
        return event_id

    async def update_session_config_key(
        self,
        session_id: UUID,
        key: str,
        value: dict[str, Any],
    ) -> None:
        self.config_updates.append((session_id, key, value))

    async def clear_session_config_key(self, session_id: UUID, key: str) -> None:
        self.config_clears.append((session_id, key))

    async def emit_synthetic_user_message(
        self,
        session_id: UUID,
        *,
        content: str,
        synthetic: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self.synthetic_messages.append((session_id, content, synthetic, metadata))
        event_id = self.next_event_id
        self.next_event_id += 1
        return event_id

    async def advance_harness_cursor(
        self,
        session_id: UUID,
        through_event_id: int,
        lease_token: UUID,
    ) -> None:
        self.cursor_advances.append({
            "session_id": session_id,
            "through_event_id": through_event_id,
            "lease_token": lease_token,
        })


class FakeRedis:
    def __init__(self) -> None:
        self.zadds: list[tuple[str, dict[str, float]]] = []

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zadds.append((key, mapping))


def _make_harness(store: FakeStore, **overrides: Any) -> AgentHarness:
    tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
    )
    defaults: dict[str, Any] = {
        "session_store": store,
        "tool_registry": ToolRegistry(),
        "llm_client": AsyncMock(),
        "tenant": tenant,
        "worker_id": "worker-a",
        "budget": IterationBudget(max_total=90),
        "context_compressor": MagicMock(spec=ContextCompressor),
        "prompt_builder": MagicMock(spec=PromptBuilder),
        "redis_client": None,
    }
    defaults.update(overrides)
    return AgentHarness(**defaults)


def _active_outcome_config() -> dict[str, Any]:
    state = start_outcome(
        "Fix tests",
        rubric="pytest passes",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00+00:00",
    )
    return state.to_config()


@pytest.mark.asyncio
async def test_handle_goal_set_persists_state_and_kicks_off_work() -> None:
    store = FakeStore()
    harness = _make_harness(store)
    session = _session()
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal Fix all tests", lease)

    assert store.config_updates[0][1] == "outcome"
    state = store.config_updates[0][2]
    assert state["description"] == "Fix all tests"
    assert state["status"] == "active"
    assert any(event[1] == EventType.OUTCOME_DEFINED for event in store.events)
    response = [event for event in store.events if event[1] == EventType.LLM_RESPONSE][-1]
    assert "Outcome defined" in response[2]["message"]["content"]
    assert store.synthetic_messages == [
        (
            session.id,
            "Fix all tests",
            "outcome_kickoff",
            {"outcome_id": state["id"]},
        ),
    ]
    response_event_id = store.next_event_id - 2
    assert store.cursor_advances[-1]["through_event_id"] == response_event_id


@pytest.mark.asyncio
async def test_handle_goal_status_without_goal_reports_no_outcome() -> None:
    store = FakeStore()
    harness = _make_harness(store)
    session = _session()
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal status", lease)

    response = [event for event in store.events if event[1] == EventType.LLM_RESPONSE][-1]
    assert "No active outcome" in response[2]["message"]["content"]
    assert store.synthetic_messages == []


@pytest.mark.asyncio
async def test_handle_goal_pause_updates_existing_state() -> None:
    store = FakeStore()
    harness = _make_harness(store)
    session = _session(config={
        "outcome": {
            "id": "outc_test",
            "description": "Fix tests",
            "rubric": "pytest passes",
            "status": "active",
            "iteration": 1,
            "max_iterations": 5,
        },
    })
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal pause", lease)

    state = store.config_updates[0][2]
    assert state["status"] == "paused"
    assert state["paused_reason"] == "user-paused"
    assert any(event[1] == EventType.OUTCOME_PAUSED for event in store.events)


@pytest.mark.asyncio
async def test_handle_goal_clear_marks_config_cleared() -> None:
    store = FakeStore()
    harness = _make_harness(store)
    session = _session(config={
        "outcome": {
            "id": "outc_test",
            "description": "Fix tests",
            "rubric": "pytest passes",
            "status": "active",
            "iteration": 1,
            "max_iterations": 5,
        },
    })
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal clear", lease)

    assert store.config_clears == [(session.id, "outcome")]
    assert any(event[1] == EventType.OUTCOME_CLEARED for event in store.events)


@pytest.mark.asyncio
async def test_post_turn_outcome_evaluation_enqueues_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    redis = FakeRedis()
    harness = _make_harness(store, redis_client=redis)
    session = _session(config={"outcome": _active_outcome_config()})
    lease = _lease(session.id)

    async def fake_evaluate(
        *,
        state: Any,
        latest_response: str,
        model: str,
    ) -> Any:
        return parse_outcome_evaluation(
            '{"result":"needs_revision","explanation":"Tests still fail",'
            '"feedback":"Run pytest"}',
        )

    monkeypatch.setattr(harness, "_evaluate_outcome", fake_evaluate)

    handled = await harness._maybe_continue_outcome(
        session,
        lease,
        latest_response="I fixed one test",
        response_event_id=10,
        model="gpt-4o",
    )

    assert handled is True
    assert store.config_updates[-1][2]["status"] == "active"
    assert any(event[1] == EventType.OUTCOME_EVALUATION_START for event in store.events)
    assert any(event[1] == EventType.OUTCOME_EVALUATION_ONGOING for event in store.events)
    assert any(event[1] == EventType.OUTCOME_EVALUATION_END for event in store.events)
    assert any(event[1] == EventType.OUTCOME_CONTINUATION for event in store.events)
    synthetic = store.synthetic_messages[-1]
    assert synthetic[2] == "outcome_continuation"
    assert "Run pytest" in synthetic[1]
    assert redis.zadds
    assert store.cursor_advances[-1]["through_event_id"] == store.next_event_id - 2


@pytest.mark.asyncio
async def test_post_turn_outcome_evaluation_completes_when_satisfied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    harness = _make_harness(store)
    session = _session(config={"outcome": _active_outcome_config()})
    lease = _lease(session.id)

    async def fake_evaluate(
        *,
        state: Any,
        latest_response: str,
        model: str,
    ) -> Any:
        return parse_outcome_evaluation(
            '{"result":"satisfied","explanation":"pytest passes","feedback":""}',
        )

    monkeypatch.setattr(harness, "_evaluate_outcome", fake_evaluate)

    handled = await harness._maybe_continue_outcome(
        session,
        lease,
        latest_response="All tests pass",
        response_event_id=10,
        model="gpt-4o",
    )

    assert handled is False
    assert store.config_updates[-1][2]["status"] == "satisfied"
    assert store.synthetic_messages == []


@pytest.mark.asyncio
async def test_evaluate_outcome_uses_base_llm_model_by_default() -> None:
    store = FakeStore()
    llm = MagicMock()
    llm.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(
                content=(
                    '{"result":"satisfied","explanation":"done",'
                    '"feedback":"done"}'
                ),
            )),
        ],
    ))
    harness = _make_harness(store, llm_client=llm)
    harness._outcome_settings = lambda: SimpleNamespace(evaluator_model="")
    state = start_outcome(
        "Fix tests",
        rubric="pytest passes",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00+00:00",
    )

    evaluation = await harness._evaluate_outcome(
        state=state,
        latest_response="All tests pass",
        model="base-model",
    )

    assert evaluation.result == "satisfied"
    call = llm.chat.completions.create.await_args.kwargs
    assert call["model"] == "base-model"
    assert call["temperature"] == 0
