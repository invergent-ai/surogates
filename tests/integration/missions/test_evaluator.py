"""Integration tests for the mission evaluator trigger logic."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from surogates.db.models import Task
from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore


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
