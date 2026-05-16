"""Schema tests for Task and TaskLink ORM models.

DB-backed: lives under ``tests/integration/`` to inherit the testcontainers
``session_factory`` / ``engine`` fixtures from
``tests/integration/conftest.py``.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from surogates.db.models import Session as ORMSession, Task, TaskLink

from tests.integration.conftest import create_org


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    """Insert a throwaway org per test for FK satisfaction."""
    return await create_org(session_factory)


@pytest.mark.asyncio(loop_scope="session")
async def test_task_round_trip(session_factory, org_id: uuid.UUID):
    """A Task row persists and round-trips with expected defaults."""
    parent_session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=parent_session_id, org_id=org_id, agent_id="agent-a",
            channel="web", status="active",
        ))
        await db.flush()
        task = Task(
            org_id=org_id,
            parent_session_id=parent_session_id,
            goal="research the postgres migration",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id
        task_status = task.status
        task_attempt_count = task.attempt_count
        task_max_attempts = task.max_attempts

    assert task_status == "todo"
    assert task_attempt_count == 0
    assert task_max_attempts == 3
    assert task_id is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_task_link_unique(session_factory, org_id: uuid.UUID):
    """task_links (parent_id, child_id) is the PK and rejects duplicates."""
    parent_session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=parent_session_id, org_id=org_id, agent_id="agent-a",
            channel="web", status="active",
        ))
        p = Task(org_id=org_id, parent_session_id=parent_session_id, goal="p")
        c = Task(org_id=org_id, parent_session_id=parent_session_id, goal="c")
        db.add_all([p, c])
        await db.flush()
        db.add(TaskLink(parent_id=p.id, child_id=c.id))
        await db.commit()
        p_id = p.id
        c_id = c.id

    async with session_factory() as db:
        db.add(TaskLink(parent_id=p_id, child_id=c_id))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_sessions_task_id_nullable_fk(session_factory, org_id: uuid.UUID):
    """sessions.task_id is nullable and FKs to tasks(id)."""
    parent_session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=parent_session_id, org_id=org_id, agent_id="agent-a",
            channel="web", status="active",
        ))
        task = Task(org_id=org_id, parent_session_id=parent_session_id, goal="g")
        db.add(task)
        await db.flush()
        child_id = uuid.uuid4()
        child = ORMSession(
            id=child_id, org_id=org_id, agent_id="agent-a",
            channel="task", status="active", task_id=task.id,
        )
        db.add(child)
        await db.commit()

    async with session_factory() as db:
        # Fetch fresh in a new session to avoid lazy-load on expired attrs.
        from sqlalchemy import select
        row = (await db.execute(
            select(ORMSession).where(ORMSession.id == child_id),
        )).scalar_one()
        assert row.task_id is not None
