"""Schema tests for the Mission ORM model + tasks.mission_id column."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from surogates.db.models import (
    Mission,
    Session as ORMSession,
    Task,
)

from tests.integration.conftest import create_org, create_user


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(session_factory, org_id) -> uuid.UUID:
    return await create_user(session_factory, org_id)


@pytest_asyncio.fixture(loop_scope="session")
async def chat_session(session_factory, org_id, user_id):
    sid = uuid.uuid4()
    async with session_factory() as db:
        s = ORMSession(
            id=sid, org_id=org_id, user_id=user_id, agent_id="orchestrator",
            channel="web", status="active",
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


@pytest.mark.asyncio(loop_scope="session")
async def test_mission_round_trip_with_defaults(session_factory, org_id, user_id, chat_session):
    """Mission row persists with the documented defaults."""
    async with session_factory() as db:
        db.add(Mission(
            id=uuid.uuid4(),
            org_id=org_id,
            user_id=user_id,
            session_id=chat_session.id,
            agent_id="orchestrator",
            description="Train a 0.6B model and hit 0.8 on gsm8k",
            rubric="A verifier task must report result_metadata.score >= 0.8",
        ))
        await db.commit()

    async with session_factory() as db:
        m = (await db.execute(
            select(Mission).where(Mission.session_id == chat_session.id)
        )).scalar_one()
        assert m.status == "active"
        assert m.iteration == 0
        assert m.max_iterations == 20
        assert m.evaluator_parse_failures == 0
        assert m.last_evaluation_result is None
        assert m.last_evaluation_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_tasks_mission_id_fk(session_factory, org_id, user_id, chat_session):
    """tasks.mission_id is nullable and FKs to missions(id)."""
    async with session_factory() as db:
        mission = Mission(
            id=uuid.uuid4(), org_id=org_id, user_id=user_id,
            session_id=chat_session.id, agent_id="orchestrator",
            description="g", rubric="r",
        )
        db.add(mission)
        await db.flush()
        task = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="research", status="ready", mission_id=mission.id,
        )
        db.add(task)
        await db.commit()
        tid = task.id
        mid = mission.id

    async with session_factory() as db:
        loaded = await db.get(Task, tid)
        assert loaded.mission_id == mid


@pytest.mark.asyncio(loop_scope="session")
async def test_tasks_mission_id_defaults_null(session_factory, org_id, chat_session):
    """A non-mission Task has mission_id == None."""
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="solo", status="ready",
        )
        db.add(t)
        await db.commit()
        tid = t.id
    async with session_factory() as db:
        loaded = await db.get(Task, tid)
        assert loaded.mission_id is None
