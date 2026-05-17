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
    kickoff message with stale config and ``spawn_task`` gets filtered
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
    assert session.config["active_mission_id"] == str(mission_id)
    assert "subagent-task-orchestrator" in session.config["preloaded_skills"]


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
async def test_mission_has_pending_work_returns_false_without_session_factory(
) -> None:
    """No session_factory wired => fall back to allowing completion. The
    helper must never raise; a False return preserves the pre-mission
    completion behaviour for harnesses that aren't DB-backed (test rigs)."""
    store = FakeStore()
    harness = _make_harness(store)
    # _make_harness leaves session_factory unset; assert the helper
    # short-circuits without trying to import MissionStore.
    assert (
        await harness._mission_has_pending_work(uuid4()) is False
    )


def test_parse_judge_json_strips_fenced_markdown_block() -> None:
    from surogates.harness.loop import _parse_judge_json

    raw = """```json
{"result": "satisfied", "explanation": "ok", "feedback": ""}
```"""
    assert _parse_judge_json(raw) == {
        "result": "satisfied",
        "explanation": "ok",
        "feedback": "",
    }


def test_parse_judge_json_extracts_object_from_prose() -> None:
    from surogates.harness.loop import _parse_judge_json

    raw = (
        "Let me think about this carefully. The verifier returned 244. "
        'The rubric required >= 4. Therefore:\n\n'
        '{"result": "satisfied", "explanation": "244 >= 4", "feedback": ""}'
    )
    parsed = _parse_judge_json(raw)
    assert parsed["result"] == "satisfied"


def test_parse_judge_json_rejects_empty() -> None:
    import pytest

    from surogates.harness.loop import _parse_judge_json

    with pytest.raises(ValueError):
        _parse_judge_json("")
    with pytest.raises(ValueError):
        _parse_judge_json("   \n\n")


def test_parse_judge_json_rejects_non_object() -> None:
    import pytest

    from surogates.harness.loop import _parse_judge_json

    with pytest.raises(ValueError, match="non-object"):
        _parse_judge_json('"just a string"')


def test_judge_prefers_structured_generation_when_outlines_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``generate_structured`` returns a typed verdict, the judge
    returns its dump without falling back to the chat-completion path —
    so a misbehaving raw chat completion (empty content, broken JSON)
    can't override a successful structured result."""
    import asyncio

    from surogates.harness.loop import _MissionVerdict, _build_mission_judge

    async def fake_generate_structured(**kwargs: Any) -> _MissionVerdict:
        return _MissionVerdict(
            result="satisfied", explanation="rubric met", feedback="",
        )

    monkeypatch.setattr(
        "surogates.harness.loop.generate_structured", fake_generate_structured,
    )

    class ExplodingClient:
        @property
        def chat(self):
            raise AssertionError(
                "fallback chat completion must NOT run when "
                "structured generation succeeded",
            )

    judge = _build_mission_judge(
        llm_client=ExplodingClient(), eval_model="m",
    )
    assert asyncio.run(judge("sys", "user")) == {
        "result": "satisfied",
        "explanation": "rubric met",
        "feedback": "",
    }


def test_judge_fallback_rejects_malformed_verdict_shape() -> None:
    """The fallback JSON parser validates against the verdict schema —
    a JSON object with an invalid ``result`` value must raise so the
    evaluator records a parse failure rather than passing a bogus
    verdict to ``apply_verdict``."""
    import asyncio

    from surogates.harness.loop import (
        MissionJudgeParseError,
        _build_mission_judge,
    )

    class FakeMessage:
        content = '{"result": "not-a-real-status", "explanation": "x", "feedback": ""}'

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeChatCompletions:
        async def create(self, **kwargs):
            return FakeResponse()

    class FakeChat:
        completions = FakeChatCompletions()

    class FakeClient:
        chat = FakeChat()

    judge = _build_mission_judge(llm_client=FakeClient(), eval_model="m")
    with pytest.raises(MissionJudgeParseError):
        asyncio.run(judge("sys", "user"))


def test_judge_falls_back_to_reasoning_content_when_content_empty() -> None:
    """Reasoning-mode models (GLM, DeepSeek) sometimes leave
    ``content`` empty and put the answer in ``reasoning_content``.
    The judge must read the fallback rather than parse-failing."""
    import asyncio

    from surogates.harness.loop import _build_mission_judge

    class FakeMessage:
        content = ""
        reasoning_content = (
            '{"result": "satisfied", "explanation": "ok", "feedback": ""}'
        )

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeChatCompletions:
        async def create(self, **kwargs):
            return FakeResponse()

    class FakeChat:
        completions = FakeChatCompletions()

    class FakeClient:
        chat = FakeChat()

    judge = _build_mission_judge(llm_client=FakeClient(), eval_model="m")
    verdict = asyncio.run(judge("sys", "user"))
    assert verdict == {
        "result": "satisfied",
        "explanation": "ok",
        "feedback": "",
    }


@pytest.mark.asyncio
async def test_mission_has_pending_work_false_when_no_active_mission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active session_factory but no active mission for this session
    must return False so /chat sessions (no mission) complete normally."""
    from surogates.missions.models import Mission as PydMission

    store = FakeStore()
    harness = _make_harness(store, session_factory=MagicMock())

    async def fake_get_active_for_session(self: Any, sid: UUID) -> PydMission | None:
        return None

    monkeypatch.setattr(
        "surogates.missions.store.MissionStore.get_active_for_session",
        fake_get_active_for_session,
    )
    assert (
        await harness._mission_has_pending_work(uuid4()) is False
    )


@pytest.mark.asyncio
async def test_handle_mission_create_rejects_service_account_principal() -> None:
    """A tenant with user_id=None (service-account/channel session) must
    not be allowed to create a mission — missions.user_id is NOT NULL
    and a bare attempt to insert would surface as NotNullViolationError."""
    store = FakeStore()
    # Override the harness tenant to have user_id=None (service account).
    sa_tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=None,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
        service_account_id=UUID("00000000-0000-0000-0000-000000000099"),
    )
    harness = _make_harness(
        store, tenant=sa_tenant, redis_client=AsyncMock(),
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
    assert "user session" in content
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
