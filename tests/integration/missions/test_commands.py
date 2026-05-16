"""Integration tests for /mission slash command handlers."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from surogates.db.models import Event, Session as ORMSession
from surogates.session.events import EventType


@pytest.mark.asyncio(loop_scope="session")
async def test_create_inserts_mission_emits_event_and_kickoff(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A successful /mission create writes a Mission row, emits
    mission.defined, emits a synthetic kickoff user.message, and updates
    session.config with active_mission_id + coordinator=True + the
    preloaded orchestrator skill."""
    from surogates.missions.commands import handle_mission_create
    from surogates.missions.store import MissionStore

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    store = MissionStore(session_factory)
    result = await handle_mission_create(
        description="Train 0.6B model",
        rubric="gsm8k >= 0.8 (verifier reports result_metadata.score)",
        session_id=chat_session.id,
        user_id=user_id,
        org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store,
        redis=redis,
    )

    assert result.ok is True
    mid = result.mission_id

    m = await store.get(mid)
    assert m.status == "active"
    assert m.description == "Train 0.6B model"

    async with session_factory() as db:
        defined = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.MISSION_DEFINED.value,
            )
        )).scalars().all()
        assert len(defined) == 1
        assert defined[0].data["mission_id"] == str(mid)

        kickoffs = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.USER_MESSAGE.value,
            )
        )).scalars().all()
        assert any(
            ev.data.get("synthetic") == "mission_kickoff"
            for ev in kickoffs
        )

        sess = await db.get(ORMSession, chat_session.id)
        assert sess.config["active_mission_id"] == str(mid)
        assert sess.config["coordinator"] is True
        preloaded = sess.config.get("preloaded_skills") or []
        assert "subagent-task-orchestrator" in preloaded

    redis.zadd.assert_called_once()


@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_when_active_goal_present(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """If session.config has a non-terminal /goal outcome, /mission create fails."""
    from surogates.missions.commands import handle_mission_create
    from surogates.missions.store import MissionStore

    async with session_factory() as db:
        sess = await db.get(ORMSession, chat_session.id)
        cfg = dict(sess.config or {})
        cfg["outcome"] = {
            "id": "outc_x", "status": "active",
            "description": "...", "rubric": "...",
            "iteration": 0, "max_iterations": 20,
        }
        sess.config = cfg
        await db.commit()

    result = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id,
        user_id=user_id, org_id=org_id, agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=MissionStore(session_factory),
        redis=AsyncMock(zadd=AsyncMock()),
    )
    assert result.ok is False
    assert "goal" in result.error.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_when_active_mission_already_on_session(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import handle_mission_create
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock())

    await handle_mission_create(
        description="first", rubric="r",
        session_id=chat_session.id,
        user_id=user_id, org_id=org_id, agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    second = await handle_mission_create(
        description="second", rubric="r2",
        session_id=chat_session.id,
        user_id=user_id, org_id=org_id, agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    assert second.ok is False
    assert "mission" in second.error.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_status_returns_active_mission_summary(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_status,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    status = await handle_mission_status(
        session_id=chat_session.id, mission_store=store,
    )
    assert status.ok is True
    assert str(created.mission_id) in status.message
    assert "active" in status.message


@pytest.mark.asyncio(loop_scope="session")
async def test_status_when_no_active_mission(
    session_factory, session_store, org_id, user_id,
):
    from surogates.missions.commands import handle_mission_status
    from surogates.missions.store import MissionStore

    fresh = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=fresh, org_id=org_id, user_id=user_id, agent_id="orchestrator",
            channel="web", status="active",
        ))
        await db.commit()

    status = await handle_mission_status(
        session_id=fresh, mission_store=MissionStore(session_factory),
    )
    assert status.ok is True
    assert "no active mission" in status.message.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_pause_transitions_status_and_emits_event(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_pause,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock())
    await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    result = await handle_mission_pause(
        session_id=chat_session.id, reason="waiting on review",
        session_store=session_store, mission_store=store,
    )
    assert result.ok is True
    m = await store.get(result.mission_id)
    assert m.status == "paused"
    assert m.paused_reason == "waiting on review"

    async with session_factory() as db:
        evs = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.MISSION_PAUSED.value,
            )
        )).scalars().all()
        assert len(evs) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_resume_transitions_back_to_active(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_pause, handle_mission_resume,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock())
    await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    await handle_mission_pause(
        session_id=chat_session.id, reason="x",
        session_store=session_store, mission_store=store,
    )
    res = await handle_mission_resume(
        session_id=chat_session.id, agent_id="orchestrator",
        session_store=session_store, mission_store=store, redis=redis,
    )
    assert res.ok is True
    m = await store.get(res.mission_id)
    assert m.status == "active"


@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_without_cascade_marks_cancelled(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_cancel,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock(), publish=AsyncMock())
    await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    res = await handle_mission_cancel(
        session_id=chat_session.id,
        reason="user changed mind",
        cascade_to_workers=False,
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    assert res.ok is True
    m = await store.get(res.mission_id)
    assert m.status == "cancelled"
    assert m.cancelled_reason == "user changed mind"
    redis.publish.assert_not_called()


@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_with_cascade_publishes_interrupt_per_running_worker(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """cascade_to_workers=True publishes an interrupt for each running task
    and marks every non-terminal mission task as cancelled."""
    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.db.models import Task
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_cancel,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock(), publish=AsyncMock())
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )

    worker_session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=worker_session_id, org_id=org_id, user_id=user_id,
            agent_id="orchestrator", channel="task", status="active",
        ))
        await db.flush()
        running = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="train", status="running", mission_id=created.mission_id,
            current_session_id=worker_session_id, attempt_count=1,
        )
        ready = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="eval", status="ready", mission_id=created.mission_id,
        )
        done = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="research", status="done", mission_id=created.mission_id,
        )
        db.add_all([running, ready, done])
        await db.commit()
        running_id = running.id
        ready_id = ready.id
        done_id = done.id

    res = await handle_mission_cancel(
        session_id=chat_session.id,
        reason="abort",
        cascade_to_workers=True,
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    assert res.ok is True

    channels_published = [c.args[0] for c in redis.publish.call_args_list]
    assert f"{INTERRUPT_CHANNEL_PREFIX}:{worker_session_id}" in channels_published

    async with session_factory() as db:
        for tid, expected in (
            (running_id, "cancelled"),
            (ready_id, "cancelled"),
            (done_id, "done"),
        ):
            t = await db.get(Task, tid)
            assert t.status == expected
