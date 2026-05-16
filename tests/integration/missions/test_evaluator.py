"""Integration tests for the mission evaluator trigger logic."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from surogates.db.models import Event, Session as ORMSession, Task
from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore
from surogates.session.events import EventType


@pytest.mark.asyncio(loop_scope="session")
async def test_trigger_on_task_terminal_event(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A mission task transitioning to done makes should_evaluate return True."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )

    async with session_factory() as db:
        db.add(Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="t", status="done",
            completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            mission_id=created.mission_id,
        ))
        await db.commit()

    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="I queued some work.",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=30,
    )
    assert decision.should is True
    assert decision.trigger == "task_terminal"


@pytest.mark.asyncio(loop_scope="session")
async def test_trigger_on_completion_marker(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """An explicit [[mission-complete]] marker triggers evaluation."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )

    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="Done.\n[[mission-complete]]",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=30,
    )
    assert decision.should is True
    assert decision.trigger == "completion_claim"


@pytest.mark.asyncio(loop_scope="session")
async def test_no_trigger_on_plain_response_without_terminal_task(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """The /goal rule (every no-tool-call response) must NOT apply here."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="Thinking about how to proceed.",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=30,
    )
    assert decision.should is False


@pytest.mark.asyncio(loop_scope="session")
async def test_rate_limit_blocks_within_window(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A recently-evaluated mission is skipped even if a trigger fires."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    await store.record_evaluation(
        created.mission_id, result="needs_revision",
        explanation="", feedback="",
    )

    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="[[mission-complete]]",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=30,
    )
    assert decision.should is False
    assert decision.trigger == "rate_limited"


@pytest.mark.asyncio(loop_scope="session")
async def test_old_terminal_task_does_not_retrigger_after_evaluation(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A terminal task that was already evaluated does not retrigger forever."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with session_factory() as db:
        db.add(Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="old verifier", status="done",
            completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            mission_id=created.mission_id,
        ))
        await db.commit()

    await store.record_evaluation(
        created.mission_id, result="needs_revision",
        explanation="", feedback="",
    )

    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="plain coordinator response",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=0,
    )
    assert decision.should is False
    assert decision.trigger == "no_trigger"


@pytest.mark.asyncio(loop_scope="session")
async def test_build_evaluator_prompt_includes_all_four_blocks(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """The evaluator prompt carries rubric, response, completed tasks, in-flight tasks."""
    from surogates.missions.evaluator import build_evaluator_prompt

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="train model", rubric="gsm8k >= 0.8",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with session_factory() as db:
        db.add_all([
            Task(
                org_id=org_id, parent_session_id=chat_session.id,
                goal="research vLLM", status="done",
                result="vLLM cheaper at our scale",
                result_metadata={"sources": 5},
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                mission_id=created.mission_id,
            ),
            Task(
                org_id=org_id, parent_session_id=chat_session.id,
                goal="verifier-round-1", status="done",
                result="gsm8k=0.65 over 200 examples",
                result_metadata={"score": 0.65, "n": 200},
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                mission_id=created.mission_id,
            ),
            Task(
                org_id=org_id, parent_session_id=chat_session.id,
                goal="training-round-2", status="running",
                attempt_count=1, mission_id=created.mission_id,
            ),
        ])
        await db.commit()

    prompt = await build_evaluator_prompt(
        mission_id=created.mission_id,
        coordinator_last_response="Round 1 done; running round 2.",
        session_factory=session_factory,
        mission_store=store,
    )
    assert "gsm8k >= 0.8" in prompt
    assert "Round 1 done" in prompt
    assert "vLLM cheaper" in prompt
    assert "0.65" in prompt
    assert "training-round-2" in prompt
    assert "running" in prompt


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_verdict_satisfied_marks_status_terminal(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import apply_verdict

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    await apply_verdict(
        mission_id=created.mission_id,
        verdict={"result": "satisfied", "explanation": "rubric met", "feedback": ""},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="task_terminal",
    )
    m = await store.get(created.mission_id)
    assert m.status == "satisfied"
    assert m.last_evaluation_result == "satisfied"
    async with session_factory() as db:
        sess = await db.get(ORMSession, chat_session.id)
        assert "active_mission_id" not in (sess.config or {})


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_verdict_needs_revision_emits_continuation(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import apply_verdict

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    await apply_verdict(
        mission_id=created.mission_id,
        verdict={
            "result": "needs_revision",
            "explanation": "verifier shows 0.65, threshold 0.8",
            "feedback": "spawn another training round and a verifier task",
        },
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="task_terminal",
    )
    m = await store.get(created.mission_id)
    assert m.status == "active"
    assert m.iteration == 1
    assert m.last_evaluation_result == "needs_revision"
    async with session_factory() as db:
        cont = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.MISSION_CONTINUATION.value,
            )
        )).scalars().all()
        assert len(cont) == 1
        synthetic = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.USER_MESSAGE.value,
            )
        )).scalars().all()
        assert any(
            e.data.get("synthetic") == "mission_continuation" for e in synthetic
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_verdict_max_iterations_reached(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import apply_verdict

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    for _ in range(19):
        await store.increment_iteration(created.mission_id)

    await apply_verdict(
        mission_id=created.mission_id,
        verdict={"result": "needs_revision", "explanation": "", "feedback": ""},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="task_terminal",
    )
    m = await store.get(created.mission_id)
    assert m.status == "max_iterations_reached"
