"""A mission's agents are the sessions its coordinator started with ``agent`` while the mission was active.

Each carries the mission's id in its ``agent_dispatch``.  The judge, the
budget, the idle nudge and ``/mission cancel --cascade`` read them where they
read the task layer's rows.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from surogates.missions.commands import handle_mission_cancel, handle_mission_create
from surogates.missions.store import MissionStore

from tests.integration.missions.conftest import a_mission_agent

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _mission(session_factory, session_store, org_id, user_id, chat_session):
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id, agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory, mission_store=store,
    )
    return store, created.mission_id


def _later() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(seconds=5)).replace(tzinfo=None)


async def _start_an_agent(session_store, session_factory, session_id, org_id, user_id) -> uuid.UUID:
    from surogates.tools.builtin import agents

    started = json.loads(await agents._agent_handler(
        {"description": "Research", "prompt": "Find the numbers.", "run_in_background": True},
        session_store=session_store, session_factory=session_factory, redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id, user_id=user_id), session_id=str(session_id), budget=None,
    ))
    assert "error" not in started, started
    return uuid.UUID(started["agent_id"])


async def test_an_agent_the_coordinator_starts_carries_the_mission(
    session_factory, session_store, org_id, user_id, chat_session,
):
    store, mission_id = await _mission(session_factory, session_store, org_id, user_id, chat_session)
    agent_id = await _start_an_agent(session_store, session_factory, chat_session.id, org_id, user_id)
    agent = await session_store.get_session(agent_id)
    assert agent.config["agent_dispatch"]["mission_id"] == str(mission_id)
    assert [a.session_id for a in await store.agents(mission_id)] == [agent_id]


async def test_an_agent_started_outside_a_mission_is_no_mission_agent(
    session_factory, session_store, org_id, user_id, chat_session,
):
    agent_id = await _start_an_agent(session_store, session_factory, chat_session.id, org_id, user_id)
    agent = await session_store.get_session(agent_id)
    assert "mission_id" not in agent.config["agent_dispatch"]


async def test_the_judge_waits_while_a_mission_agent_runs(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import should_evaluate

    store, mission_id = await _mission(session_factory, session_store, org_id, user_id, chat_session)
    await a_mission_agent(session_factory, chat_session, mission_id, status="completed", updated_at=_later())
    await a_mission_agent(session_factory, chat_session, mission_id, status="active")
    decision = await should_evaluate(
        mission_id=mission_id, coordinator_last_response="[[mission-complete]]",
        session_factory=session_factory, mission_store=store, rate_limit_seconds=0,
    )
    assert (decision.should, decision.trigger) == (False, "agents_in_flight")


async def test_the_judge_fires_once_a_mission_agent_ended(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import should_evaluate

    store, mission_id = await _mission(session_factory, session_store, org_id, user_id, chat_session)
    await a_mission_agent(session_factory, chat_session, mission_id, status="failed", updated_at=_later())
    decision = await should_evaluate(
        mission_id=mission_id, coordinator_last_response="Still working.",
        session_factory=session_factory, mission_store=store, rate_limit_seconds=0,
    )
    assert (decision.should, decision.trigger) == (True, "agent_ended")


async def test_satisfied_is_demoted_while_no_mission_agent_completed(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.verdict_policy import adjust_mission_verdict

    store, mission_id = await _mission(session_factory, session_store, org_id, user_id, chat_session)
    await a_mission_agent(session_factory, chat_session, mission_id, status="failed")
    done, total = await store.agent_counts(mission_id)
    assert (done, total) == (0, 1)
    verdict = adjust_mission_verdict({"result": "satisfied"}, completed_agents=done, total_agents=total)
    assert verdict["result"] == "needs_revision"
    await a_mission_agent(session_factory, chat_session, mission_id, status="completed")
    done, total = await store.agent_counts(mission_id)
    assert adjust_mission_verdict({"result": "satisfied"}, completed_agents=done, total_agents=total)["result"] == "satisfied"


async def test_tokens_count_the_coordinator_and_its_agents_trees(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.db.models import Session as ORMSession

    store, mission_id = await _mission(session_factory, session_store, org_id, user_id, chat_session)
    before = await store.tokens_spent(mission_id)
    agent = await a_mission_agent(session_factory, chat_session, mission_id, tokens=(100, 20))
    # The agent's own agent counts through it.
    async with session_factory() as db:
        db.add(ORMSession(
            id=uuid.uuid4(), org_id=org_id, user_id=user_id, agent_id="orchestrator", parent_id=agent.id,
            channel="delegation", status="completed", config={}, input_tokens=7, output_tokens=3,
        ))
        await db.commit()
    # An agent of the coordinator's that is not the mission's does not.
    await a_mission_agent(session_factory, chat_session, uuid.uuid4(), tokens=(1000, 1000))
    assert await store.tokens_spent(mission_id) == before + 130


async def test_cascade_cancel_stops_running_agents_and_theirs(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.db.models import Session as ORMSession

    store, mission_id = await _mission(session_factory, session_store, org_id, user_id, chat_session)
    running = await a_mission_agent(session_factory, chat_session, mission_id, status="active")
    finished = await a_mission_agent(session_factory, chat_session, mission_id, status="completed")
    below = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=below, org_id=org_id, user_id=user_id, agent_id="orchestrator", parent_id=running.id,
            channel="delegation", status="active",
            config={"agent_dispatch": {"mode": "foreground", "description": "Below", "parent": str(running.id)}},
        ))
        await db.commit()
    redis = AsyncMock()
    result = await handle_mission_cancel(
        session_id=chat_session.id, reason="enough", cascade_to_workers=True,
        session_store=session_store, session_factory=session_factory, mission_store=store, redis=redis,
    )
    assert result.ok, result.error
    assert (await session_store.get_session(running.id)).status == "paused"
    assert (await session_store.get_session(below)).status == "paused"
    assert (await session_store.get_session(finished.id)).status == "completed"
    assert (await store.get(mission_id)).status == "cancelled"


async def test_an_idle_mission_waiting_on_its_agents_is_not_nudged(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from sqlalchemy import update

    from surogates.db.models import Mission as MissionRow
    from surogates.missions.nudge import nudge_idle_missions

    store, mission_id = await _mission(session_factory, session_store, org_id, user_id, chat_session)
    await a_mission_agent(session_factory, chat_session, mission_id, status="active")
    async with session_factory() as db:
        await db.execute(update(MissionRow).where(MissionRow.id == mission_id).values(
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
        ))
        await db.commit()
    before = await session_store.count_synthetic_since(
        chat_session.id, synthetic="mission_nudge", since=datetime(2000, 1, 1),
    )
    await nudge_idle_missions(session_factory=session_factory, session_store=session_store, redis=AsyncMock())
    after = await session_store.count_synthetic_since(
        chat_session.id, synthetic="mission_nudge", since=datetime(2000, 1, 1),
    )
    assert after == before


async def test_the_judge_prompt_shows_each_completed_agents_report(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import build_evaluator_prompt

    store, mission_id = await _mission(session_factory, session_store, org_id, user_id, chat_session)
    await a_mission_agent(session_factory, chat_session, mission_id, status="completed", description="Count rows",
                          agent_type="analyst", report="There are 12 rows.")
    await a_mission_agent(session_factory, chat_session, mission_id, status="active", description="Check totals")
    prompt = await build_evaluator_prompt(
        mission_id=mission_id, coordinator_last_response="Done.",
        session_factory=session_factory, mission_store=store,
    )
    assert "Completed mission agents (1)" in prompt and "In-flight mission agents (1)" in prompt
    assert "Count rows" in prompt and "analyst" in prompt and "There are 12 rows." in prompt
    assert "Check totals" in prompt
