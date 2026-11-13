from __future__ import annotations

from datetime import datetime, timezone
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
        *,
        lease_token: Any = None,
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

    async def zadd(self, key: str, mapping: dict[str, float], **_options: Any) -> None:
        # enqueue_session passes lt=True (a queued session keeps its better place)
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
    # The cursor is not the handler's to move: the wake ends the command's turn.
    assert store.cursor_advances == []


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
async def test_handle_goal_set_rejected_when_outcome_active() -> None:
    store = FakeStore()
    harness = _make_harness(store)
    existing = {
        "id": "outc_existing",
        "description": "Fix tests",
        "rubric": "pytest passes",
        "status": "active",
        "iteration": 1,
        "max_iterations": 5,
    }
    session = _session(config={"outcome": existing})
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal Ship new feature", lease)

    response = [event for event in store.events if event[1] == EventType.LLM_RESPONSE][-1]
    content = response[2]["message"]["content"]
    assert "Outcome already active" in content
    assert "/goal pause" in content and "/goal clear" in content
    # No state change, no new outcome event, no kickoff, no enqueue.
    assert store.config_updates == []
    assert not any(event[1] == EventType.OUTCOME_DEFINED for event in store.events)
    assert store.synthetic_messages == []
    assert session.config["outcome"] == existing


@pytest.mark.asyncio
async def test_handle_mission_create_propagates_config_to_in_memory_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: after /mission create, the in-memory ``session.config``
    must reflect ``coordinator=True``, ``active_mission_id``, and the
    orchestrator preload — otherwise the rest of the wake processes the
    kickoff message with stale config and the coordinator's tools get filtered
    out as a WORKER_EXCLUDED_TOOL."""
    from uuid import uuid4

    from surogates.missions.commands import MissionHandlerResult

    store = FakeStore()
    harness = _make_harness(
        store, redis_client=AsyncMock(), session_factory=MagicMock(),
    )
    session = _session()
    lease = _lease(session.id)

    mission_id = uuid4()

    async def fake_create(**_kwargs: Any) -> MissionHandlerResult:
        return MissionHandlerResult(
            ok=True, mission_id=mission_id, message="started",
            kickoff_content="[Mission kickoff]\nDescription: x\nRubric:\ny",
        )

    monkeypatch.setattr(
        "surogates.missions.commands.handle_mission_create", fake_create,
    )

    await harness._handle_mission_command(
        session,
        "/mission Train the model\n\nRubric:\nReach gsm8k >= 0.8",
        lease,
    )

    assert session.config["coordinator"] is True
    assert session.config["strict_coordinator"] is True
    assert session.config["active_mission_id"] == str(mission_id)
    assert "subagent-task-orchestrator" not in (session.config.get("preloaded_skills") or [])


@pytest.mark.asyncio
async def test_handle_mission_create_emits_kickoff_before_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PROD session 216e2577-…: the kickoff
    ``user.message`` must stay past the cursor.  Otherwise the next
    wake sees ``no actionable pending events`` and bails — the
    coordinator never starts on the mission.

    Verifies the temporal sequence:

        emit USER_MESSAGE(kickoff content)
        emit LLM_RESPONSE("Mission … started.")
        enqueue session                            # next wake fires

    and that the handler does not move the cursor.  The kickoff comes
    first: once the answer is in the log the command is not run again,
    and a worker that died between the two would leave the mission
    started and never worked on.
    """
    from uuid import uuid4

    from surogates.missions.commands import MissionHandlerResult
    from surogates.session.events import EventType

    store = FakeStore()
    redis = FakeRedis()
    harness = _make_harness(
        store, redis_client=redis, session_factory=MagicMock(),
    )
    session = _session()
    lease = _lease(session.id)

    async def fake_create(**_kwargs: Any) -> MissionHandlerResult:
        return MissionHandlerResult(
            ok=True, mission_id=uuid4(), message="Mission started.",
            kickoff_content="[Mission kickoff]\nDescription: x\nRubric:\ny",
        )

    monkeypatch.setattr(
        "surogates.missions.commands.handle_mission_create", fake_create,
    )

    await harness._handle_mission_command(
        session,
        "/mission do a thing\n\nRubric:\ndone",
        lease,
    )

    # Pull out the emitted events in order.
    emitted = [(t, data) for (_sid, t, data) in store.events]
    response_idx = next(
        i for i, (t, _) in enumerate(emitted) if t == EventType.LLM_RESPONSE
    )
    kickoff_idx = next(
        i for i, (t, d) in enumerate(emitted)
        if t == EventType.USER_MESSAGE
        and d.get("synthetic") == "mission_kickoff"
    )

    assert kickoff_idx < response_idx, (
        "kickoff user.message must be emitted before the slash response; "
        "otherwise a death between the two leaves the mission never worked on"
    )

    # The handler leaves the cursor alone, so it cannot pass the kickoff:
    # the next wake sees the kickoff as pending.
    assert store.cursor_advances == []

    # And the session must be enqueued for a fresh wake to process the kickoff.
    assert len(redis.zadds) == 1, (
        "/mission create must enqueue the session after emitting the kickoff"
    )


@pytest.mark.asyncio
async def test_handle_mission_cancel_clears_in_memory_active_mission_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: cancelling a mission must drop ``active_mission_id``
    from the in-memory session so the same wake's `/goal` mutual-exclusion
    check no longer treats the cancelled mission as in flight."""
    from uuid import uuid4

    from surogates.missions.commands import MissionHandlerResult

    store = FakeStore()
    harness = _make_harness(
        store, redis_client=AsyncMock(), session_factory=MagicMock(),
    )
    # Seed an active mission on the in-memory session.
    mission_id = uuid4()
    session = _session(
        config={
            "active_mission_id": str(mission_id),
            "coordinator": True,
        },
    )
    lease = _lease(session.id)

    async def fake_cancel(**_kwargs: Any) -> MissionHandlerResult:
        return MissionHandlerResult(
            ok=True, mission_id=mission_id, message="cancelled",
        )

    monkeypatch.setattr(
        "surogates.missions.commands.handle_mission_cancel", fake_cancel,
    )

    await harness._handle_mission_command(
        session, "/mission cancel", lease,
    )

    assert "active_mission_id" not in session.config
    # coordinator stays True for the rest of this wake — the session
    # retains its orchestrator role until the session itself terminates.


@pytest.mark.asyncio
async def test_handle_mission_create_forwards_service_account_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant with service_account_id set (Work-chat path in PROD) must
    be allowed to create a mission. The harness passes its principal
    through to handle_mission_create; the call site resolves to the SA
    owner so the row's ck_missions_one_principal CHECK is satisfied."""
    from surogates.missions.commands import MissionHandlerResult

    store = FakeStore()
    sa_id = UUID("00000000-0000-0000-0000-000000000099")
    sa_tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=None,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
        service_account_id=sa_id,
    )
    harness = _make_harness(
        store, tenant=sa_tenant, redis_client=AsyncMock(),
        session_factory=MagicMock(),
    )
    session = _session()
    lease = _lease(session.id)

    forwarded: dict[str, Any] = {}
    mission_id = uuid4()

    async def fake_create(**kwargs: Any) -> MissionHandlerResult:
        forwarded.update(kwargs)
        return MissionHandlerResult(
            ok=True, mission_id=mission_id, message="started",
        )

    monkeypatch.setattr(
        "surogates.missions.commands.handle_mission_create", fake_create,
    )

    await harness._handle_mission_command(
        session,
        "/mission Train the model\n\nRubric:\nReach gsm8k >= 0.8",
        lease,
    )

    # The harness forwarded the SA principal, not the (absent) user.
    assert forwarded.get("user_id") is None
    assert forwarded.get("service_account_id") == sa_id
    # In-memory session.config is updated so the rest of the wake sees
    # coordinator=True / strict_coordinator=True / active_mission_id (same
    # as the user path).
    assert session.config["coordinator"] is True
    assert session.config["strict_coordinator"] is True
    assert session.config["active_mission_id"] == str(mission_id)


@pytest.mark.asyncio
async def test_handle_mission_create_rejects_anonymous_channel_principal() -> None:
    """A tenant without either principal (anonymous-channel session)
    cannot own a mission — the session itself is the principal and
    a mission needs a durable owner outliving the session."""
    store = FakeStore()
    channel_tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=None,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
        service_account_id=None,
        session_scope_id=UUID("00000000-0000-0000-0000-0000000000aa"),
    )
    harness = _make_harness(
        store, tenant=channel_tenant, redis_client=AsyncMock(),
        session_factory=MagicMock(),
    )
    session = _session()
    lease = _lease(session.id)

    await harness._handle_mission_command(
        session,
        "/mission Train the model\n\nRubric:\nReach gsm8k >= 0.8",
        lease,
    )

    response = [event for event in store.events if event[1] == EventType.LLM_RESPONSE][-1]
    content = response[2]["message"]["content"]
    assert "anonymous channel" in content
    # No mission was created — config wasn't touched, no kickoff queued.
    assert store.config_updates == []
    assert store.synthetic_messages == []


@pytest.mark.asyncio
async def test_handle_goal_set_rejected_when_active_mission_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutual exclusion: /goal must refuse to set while a /mission is active."""
    store = FakeStore()
    harness = _make_harness(store)
    session = _session()
    lease = _lease(session.id)

    # Stub the mission check to simulate an active mission on the session.
    async def _stub(self: AgentHarness, sid: UUID) -> bool:
        return True
    monkeypatch.setattr(
        AgentHarness, "_session_has_active_mission", _stub,
    )

    await harness._handle_goal_command(session, "/goal Ship new feature", lease)

    response = [event for event in store.events if event[1] == EventType.LLM_RESPONSE][-1]
    content = response[2]["message"]["content"]
    assert "active /mission" in content
    # No outcome was created.
    assert store.config_updates == []
    assert not any(event[1] == EventType.OUTCOME_DEFINED for event in store.events)
    assert store.synthetic_messages == []


@pytest.mark.asyncio
async def test_handle_goal_set_allowed_when_outcome_paused() -> None:
    store = FakeStore()
    harness = _make_harness(store)
    session = _session(config={
        "outcome": {
            "id": "outc_old",
            "description": "Fix tests",
            "rubric": "pytest passes",
            "status": "paused",
            "iteration": 2,
            "max_iterations": 5,
        },
    })
    lease = _lease(session.id)

    await harness._handle_goal_command(session, "/goal Ship new feature", lease)

    state = store.config_updates[0][2]
    assert state["description"] == "Ship new feature"
    assert state["status"] == "active"
    assert any(event[1] == EventType.OUTCOME_DEFINED for event in store.events)
    assert store.synthetic_messages[0][1] == "Ship new feature"


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
async def test_outcome_continuation_does_not_complete_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    redis = FakeRedis()
    harness = _make_harness(store, redis_client=redis)
    harness._complete_session = AsyncMock()
    session = _session(config={"outcome": _active_outcome_config()})
    lease = _lease(session.id)

    async def fake_evaluate(
        *,
        state: Any,
        latest_response: str,
        model: str,
    ) -> Any:
        return parse_outcome_evaluation(
            '{"result":"needs_revision","explanation":"Missing",'
            '"feedback":"Continue"}',
        )

    monkeypatch.setattr(harness, "_evaluate_outcome", fake_evaluate)

    continued = await harness._maybe_continue_outcome(
        session,
        lease,
        latest_response="partial",
        response_event_id=10,
        model="gpt-4o",
    )

    assert continued is True
    harness._complete_session.assert_not_called()


@pytest.mark.asyncio
async def test_the_goal_evaluator_reasons_free_form_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured answer on a reasoning model comes without reasoning: the verdict is asked for free-form,
    with room to think and today's date, and the feedback reaches the agent."""
    from datetime import datetime, timezone

    from surogates.harness import loop_outcome_commands
    from surogates.harness.outcomes import EVALUATOR_MAX_TOKENS, OutcomeState

    structured = AsyncMock(return_value=None)
    monkeypatch.setattr(loop_outcome_commands, "generate_structured", structured)
    llm = AsyncMock()
    llm.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(
        content='{"explanation":"No links on any item","feedback":"Add a link to every item","result":"needs_revision"}',
    ))])
    harness = _make_harness(FakeStore(), llm_client=llm)

    evaluation = await harness._evaluate_outcome(
        state=OutcomeState.from_config(_active_outcome_config()),
        latest_response="The report.",
        model="m",
    )

    assert evaluation.result == "needs_revision" and evaluation.parse_failed is False
    assert evaluation.feedback == "Add a link to every item"
    structured.assert_not_awaited()
    asked = llm.chat.completions.create.call_args.kwargs
    assert asked["max_tokens"] == EVALUATOR_MAX_TOKENS
    assert datetime.now(timezone.utc).date().isoformat() in asked["messages"][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("free_form", ["empty", "raises"])
async def test_the_goal_evaluator_falls_back_to_a_structured_answer(
    monkeypatch: pytest.MonkeyPatch,
    free_form: str,
) -> None:
    from surogates.harness import loop_outcome_commands
    from surogates.harness.outcomes import OutcomeState, OutcomeVerdict

    async def fake_generate(**_kwargs: Any) -> OutcomeVerdict:
        return OutcomeVerdict(explanation="Met", feedback="", result="satisfied")

    monkeypatch.setattr(loop_outcome_commands, "generate_structured", fake_generate)
    llm = AsyncMock()
    if free_form == "raises":
        llm.chat.completions.create.side_effect = RuntimeError("provider down")
    else:
        llm.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content=""))])
    harness = _make_harness(FakeStore(), llm_client=llm)

    evaluation = await harness._evaluate_outcome(
        state=OutcomeState.from_config(_active_outcome_config()),
        latest_response="The report.",
        model="m",
    )

    assert evaluation.result == "satisfied" and evaluation.parse_failed is False


NO_REASON_VERDICTS = [
    '{"explanation":"...","feedback":"...","result":"satisfied"}',
    '{"explanation":"","feedback":"","result":"satisfied"}',
    '{"result":"satisfied"}',
]


@pytest.mark.parametrize("raw", NO_REASON_VERDICTS)
def test_a_verdict_without_a_reason_does_not_count(raw: str) -> None:
    from surogates.harness.outcomes import parse_outcome_evaluation

    evaluation = parse_outcome_evaluation(raw)

    assert evaluation.result == "needs_revision"
    assert evaluation.parse_failed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", NO_REASON_VERDICTS)
async def test_structured_generation_refuses_a_verdict_without_a_reason(raw: str) -> None:
    """So the evaluator moves on to its free-form attempt."""
    from surogates.harness.outcomes import OutcomeVerdict
    from surogates.harness.structured_output import _try_openai_json_mode

    llm = AsyncMock()
    llm.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=raw))],
    )

    verdict = await _try_openai_json_mode(
        llm_client=llm,
        model="m",
        messages=[{"role": "user", "content": "judge this"}],
        output_model=OutcomeVerdict,
        max_tokens=100,
        temperature=0,
    )

    assert verdict is None


@pytest.mark.asyncio
async def test_goal_with_no_reason_from_any_attempt_keeps_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from surogates.harness import loop_outcome_commands
    from surogates.harness.outcomes import OutcomeState

    async def fake_generate(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(loop_outcome_commands, "generate_structured", fake_generate)
    llm = AsyncMock()
    llm.chat.completions.create.return_value = MagicMock(choices=[MagicMock(
        message=MagicMock(content=NO_REASON_VERDICTS[0]),
    )])
    harness = _make_harness(FakeStore(), llm_client=llm)

    evaluation = await harness._evaluate_outcome(
        state=OutcomeState.from_config(_active_outcome_config()),
        latest_response="The report.",
        model="m",
    )

    assert evaluation.result == "needs_revision"
    assert evaluation.parse_failed is True


def test_verdict_reasons_come_before_the_result() -> None:
    from surogates.harness.loop_mission_evaluator import _MissionVerdict
    from surogates.harness.outcomes import OutcomeVerdict

    for model in (OutcomeVerdict, _MissionVerdict):
        schema = model.model_json_schema()
        keys = list(schema["properties"])
        assert keys.index("explanation") < keys.index("result")
        assert "explanation" in schema["required"]


@pytest.mark.asyncio
async def test_the_mission_judge_reasons_free_form_first() -> None:
    from surogates.harness.loop_mission_evaluator import _build_mission_judge
    from surogates.harness.outcomes import EVALUATOR_MAX_TOKENS

    structured = AsyncMock(return_value=None)
    llm = AsyncMock()
    llm.chat.completions.create.return_value = MagicMock(choices=[MagicMock(
        message=MagicMock(
            content='{"explanation":"Every link opened","result":"satisfied"}',
            reasoning_content=None,
        ),
    )])
    judge = _build_mission_judge(llm_client=llm, eval_model="m", structured_generator=structured)

    verdict = await judge("system", "user")

    assert verdict["result"] == "satisfied" and verdict["explanation"] == "Every link opened"
    structured.assert_not_awaited()
    assert llm.chat.completions.create.call_args.kwargs["max_tokens"] == EVALUATOR_MAX_TOKENS


@pytest.mark.asyncio
async def test_the_mission_judge_falls_back_to_a_structured_answer() -> None:
    # A reasoning model that thinks past its allowance answers nothing: the structured attempt still may.
    from surogates.harness.loop_mission_evaluator import _build_mission_judge, _MissionVerdict

    async def structured(**_kwargs: Any) -> _MissionVerdict:
        return _MissionVerdict(explanation="The verifier reported a match", result="satisfied")

    llm = AsyncMock()
    llm.chat.completions.create.return_value = MagicMock(choices=[MagicMock(
        message=MagicMock(content="", reasoning_content=None),
    )])
    judge = _build_mission_judge(llm_client=llm, eval_model="m", structured_generator=structured)

    assert (await judge("system", "user"))["result"] == "satisfied"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", NO_REASON_VERDICTS)
async def test_mission_judge_with_no_reason_is_a_parse_failure(raw: str) -> None:
    from surogates.harness.loop_mission_evaluator import (
        MissionJudgeParseError,
        _build_mission_judge,
    )

    async def no_usable_verdict(**_kwargs: Any) -> None:
        return None

    llm = AsyncMock()
    llm.chat.completions.create.return_value = MagicMock(choices=[MagicMock(
        message=MagicMock(content=raw, reasoning_content=None),
    )])
    judge = _build_mission_judge(
        llm_client=llm, eval_model="m", structured_generator=no_usable_verdict,
    )

    with pytest.raises(MissionJudgeParseError):
        await judge("system", "user")
