"""An objective must not die because the agent stopped talking.

`should_evaluate` only runs *inside* a coordinator turn. A text-only LLM
response ends that turn, and nothing re-enqueues the session — so a mission
whose coordinator goes quiet can never be re-examined. Both active missions
on the dev box were stalled this way, one of them for 48 days.

Every other mission gate is downstream of a loop this provides: budget checks
at the evaluator boundary, stagnation counts evaluations, corroboration runs
after the judge. None of them can fire if nothing wakes the coordinator.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from surogates.db.models import Mission as MissionRow
from surogates.missions.store import MissionStore
from surogates.tasks.dispatcher import nudge_idle_missions

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _idle(session_factory, mission_id, *, minutes=120):
    """Backdate the mission so it looks untouched."""
    from datetime import timedelta

    from sqlalchemy import func as safunc
    async with session_factory() as db:
        await db.execute(
            update(MissionRow).where(MissionRow.id == mission_id)
            .values(updated_at=safunc.now() - timedelta(minutes=minutes))
        )
        await db.commit()


async def _mission(session_factory, session_store, org_id, user_id):
    """Own coordinator session per test — the tick sweeps ALL active
    missions, so a shared session lets one test's mission show up in
    another's assertions."""
    sess = await session_store.create_session(
        org_id=org_id, user_id=user_id, agent_id="a", channel="web",
    )
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=sess.id,
        agent_id="a", description="ship", rubric="works",
    )
    return store, mid, sess


async def _nudges_for(session_store, session_id) -> int:
    events = await session_store.get_events(session_id)
    return sum(
        1 for e in events
        if (e.data or {}).get("synthetic") == "mission_nudge"
    )


async def test_an_idle_mission_wakes_its_coordinator(
    session_factory, session_store, org_id, user_id,
):
    """The case that stalled for 48 days."""
    store, mid, sess = await _mission(session_factory, session_store, org_id, user_id)
    await _idle(session_factory, mid)
    redis = AsyncMock()

    woken = await nudge_idle_missions(
        session_factory=session_factory, session_store=session_store,
        redis=redis, idle_seconds=60,
    )

    assert woken >= 1
    assert await _nudges_for(session_store, sess.id) == 1


async def test_a_recently_active_mission_is_left_alone(
    session_factory, session_store, org_id, user_id,
):
    """Poking a coordinator mid-thought is worse than waiting."""
    store, mid, sess = await _mission(session_factory, session_store, org_id, user_id)
    redis = AsyncMock()

    await nudge_idle_missions(
        session_factory=session_factory, session_store=session_store,
        redis=redis, idle_seconds=3600,
    )

    assert await _nudges_for(session_store, sess.id) == 0


async def test_a_live_coordinator_is_never_interrupted(
    session_factory, session_store, org_id, user_id,
):
    """A held lease means a worker is on it right now."""
    store, mid, sess = await _mission(session_factory, session_store, org_id, user_id)
    await _idle(session_factory, mid)
    await session_store.try_acquire_lease(sess.id, "worker-1")
    redis = AsyncMock()

    await nudge_idle_missions(
        session_factory=session_factory, session_store=session_store,
        redis=redis, idle_seconds=60,
    )

    assert await _nudges_for(session_store, sess.id) == 0


async def test_terminal_missions_are_ignored(
    session_factory, session_store, org_id, user_id,
):
    store, mid, sess = await _mission(session_factory, session_store, org_id, user_id)
    await store.set_status(mid, "satisfied")
    await _idle(session_factory, mid)
    redis = AsyncMock()

    await nudge_idle_missions(
        session_factory=session_factory, session_store=session_store,
        redis=redis, idle_seconds=60,
    )
    assert await _nudges_for(session_store, sess.id) == 0


async def test_nudging_is_bounded(
    session_factory, session_store, org_id, user_id,
):
    """A coordinator that keeps going quiet must not be poked forever.

    Three nudges with no evaluation in between and the mission is blocked
    for a human, not left spinning.
    """
    store, mid, sess = await _mission(session_factory, session_store, org_id, user_id)
    redis = AsyncMock()

    for _ in range(4):
        await _idle(session_factory, mid)
        await nudge_idle_missions(
            session_factory=session_factory, session_store=session_store,
            redis=redis, idle_seconds=60,
        )

    mission = await store.get(mid)
    assert mission.status == "blocked"
    assert await _nudges_for(session_store, sess.id) == 3


async def test_progress_resets_the_nudge_budget(
    session_factory, session_store, org_id, user_id,
):
    """An evaluation means the loop moved; the count starts over."""
    store, mid, sess = await _mission(session_factory, session_store, org_id, user_id)
    redis = AsyncMock()

    for _ in range(2):
        await _idle(session_factory, mid)
        await nudge_idle_missions(
            session_factory=session_factory, session_store=session_store,
            redis=redis, idle_seconds=60,
        )
    await store.record_evaluation(
        mid, result="needs_revision", explanation="keep going", feedback="f",
    )
    for _ in range(2):
        await _idle(session_factory, mid)
        await nudge_idle_missions(
            session_factory=session_factory, session_store=session_store,
            redis=redis, idle_seconds=60,
        )

    assert (await store.get(mid)).status == "active"
    assert await _nudges_for(session_store, sess.id) == 4


async def test_the_coordinator_is_told_why_it_woke(
    session_factory, session_store, org_id, user_id,
):
    store, mid, sess = await _mission(session_factory, session_store, org_id, user_id)
    await _idle(session_factory, mid)

    await nudge_idle_missions(
        session_factory=session_factory, session_store=session_store,
        redis=AsyncMock(), idle_seconds=60,
    )

    events = await session_store.get_events(sess.id)
    synthetic = [
        e for e in events
        if (e.data or {}).get("synthetic") == "mission_nudge"
    ]
    assert len(synthetic) == 1
    assert "mission" in synthetic[0].data["content"].lower()
