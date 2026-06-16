"""Integration tests for the ``spawn_task`` tool handler.

Exercise the full Task-row insert + DAG-link insert + eager-spawn path
against a real database (Testcontainers).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from surogates.db.models import (
    Session as ORMSession,
    Task,
    TaskLink,
)

from tests.integration.conftest import create_org


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def parent_session(session_factory, org_id: uuid.UUID) -> ORMSession:
    parent_id = uuid.uuid4()
    async with session_factory() as db:
        sess = ORMSession(
            id=parent_id,
            org_id=org_id,
            agent_id="orchestrator",
            channel="web",
            status="active",
            config={
                "storage_bucket": "test-bucket",
                "storage_key_prefix": "",
                "workspace_path": f"/workspace/test/{parent_id}",
                "supports_vision": False,
            },
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
    return sess


def _spawn_task_call(arguments: dict, parent_session, session_factory, *, redis=None, store=None):
    """Build the kwargs the harness would inject."""
    from surogates.tasks.tools import _spawn_task_handler

    tenant = MagicMock(org_id=parent_session.org_id, user_id=parent_session.user_id)

    # The real session_store would write to the same DB; for spawn-side
    # effects we provide a thin wrapper that uses the actual factory.
    if store is None:
        from surogates.session.store import SessionStore
        store = SessionStore(session_factory)

    if redis is None:
        redis = AsyncMock()
        redis.zadd = AsyncMock()
        redis.publish = AsyncMock()

    return _spawn_task_handler(
        arguments,
        session_store=store,
        redis=redis,
        tenant=tenant,
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_no_parents_eagerly_spawns(
    session_factory, org_id: uuid.UUID, parent_session,
):
    """With no parents, spawn_task creates Task + Session + enqueues immediately."""
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    redis.publish = AsyncMock()

    result = await _spawn_task_call(
        {"goal": "research vLLM"},
        parent_session, session_factory, redis=redis,
    )
    parsed = json.loads(result)
    assert "error" not in parsed, parsed
    assert parsed["status"] == "running"
    assert "task_id" in parsed
    assert "worker_id" in parsed

    task_id = uuid.UUID(parsed["task_id"])
    async with session_factory() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.attempt_count == 1
        assert task.current_session_id is not None
        assert task.started_at is not None

    # Enqueue happened on the shared work queue; the parent's agent id is
    # encoded into the queue member (org_id|agent_id|session_id), not the
    # queue key, after the shared-runtime refactor.
    redis.zadd.assert_called_once()
    queue_key = redis.zadd.call_args[0][0]
    assert queue_key == "surogates:work_queue"
    member_mapping = redis.zadd.call_args[0][1]
    assert any("|orchestrator|" in member for member in member_mapping)


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_with_pending_parents_stays_todo(
    session_factory, org_id: uuid.UUID, parent_session,
):
    """With at least one parent not done, the new task stays in todo."""
    async with session_factory() as db:
        parent_task = Task(
            org_id=org_id,
            parent_session_id=parent_session.id,
            goal="upstream research",
            status="running",  # not done
        )
        db.add(parent_task)
        await db.commit()
        parent_task_id = parent_task.id

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    result = await _spawn_task_call(
        {"goal": "synthesize", "parents": [str(parent_task_id)]},
        parent_session, session_factory, redis=redis,
    )
    parsed = json.loads(result)
    assert parsed["status"] == "todo"

    async with session_factory() as db:
        task = await db.get(Task, uuid.UUID(parsed["task_id"]))
        assert task.status == "todo"
        assert task.attempt_count == 0
        assert task.current_session_id is None
        # Link was inserted.
        link = await db.scalar(
            select(TaskLink).where(
                TaskLink.parent_id == parent_task_id,
                TaskLink.child_id == task.id,
            )
        )
        assert link is not None

    # No enqueue when status == todo.
    redis.zadd.assert_not_called()


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_with_all_done_parents_eagerly_spawns(
    session_factory, org_id: uuid.UUID, parent_session,
):
    """All parents done means we can spawn immediately."""
    async with session_factory() as db:
        p1 = Task(
            org_id=org_id,
            parent_session_id=parent_session.id,
            goal="p1",
            status="done",
        )
        p2 = Task(
            org_id=org_id,
            parent_session_id=parent_session.id,
            goal="p2",
            status="done",
        )
        db.add_all([p1, p2])
        await db.commit()
        p1_id = p1.id
        p2_id = p2.id

    redis = AsyncMock()
    redis.zadd = AsyncMock()
    result = await _spawn_task_call(
        {"goal": "fan-in", "parents": [str(p1_id), str(p2_id)]},
        parent_session, session_factory, redis=redis,
    )
    parsed = json.loads(result)
    assert parsed["status"] == "running"
    redis.zadd.assert_called_once()

    async with session_factory() as db:
        links = (await db.execute(
            select(TaskLink).where(TaskLink.child_id == uuid.UUID(parsed["task_id"]))
        )).scalars().all()
        assert {l.parent_id for l in links} == {p1_id, p2_id}


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_rejects_missing_goal(
    session_factory, org_id: uuid.UUID, parent_session,
):
    result = await _spawn_task_call({}, parent_session, session_factory)
    parsed = json.loads(result)
    assert "error" in parsed
    assert "goal" in parsed["error"].lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_rejects_nonexistent_parent(
    session_factory, org_id: uuid.UUID, parent_session,
):
    result = await _spawn_task_call(
        {"goal": "g", "parents": [str(uuid.uuid4())]},
        parent_session, session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "parent" in parsed["error"].lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_rejects_cross_org_parent(
    session_factory, parent_session,
):
    """A parent from a different org must be rejected — multi-tenant guard."""
    other_org_id = await create_org(session_factory)
    async with session_factory() as db:
        # Need a parent session for the foreign parent task to satisfy FK.
        foreign_parent_session_id = uuid.uuid4()
        db.add(ORMSession(
            id=foreign_parent_session_id, org_id=other_org_id,
            agent_id="other-agent", channel="web", status="active",
        ))
        foreign_task = Task(
            org_id=other_org_id,
            parent_session_id=foreign_parent_session_id,
            goal="cross-org",
            status="done",
        )
        db.add(foreign_task)
        await db.commit()
        foreign_task_id = foreign_task.id

    result = await _spawn_task_call(
        {"goal": "g", "parents": [str(foreign_task_id)]},
        parent_session, session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "org" in parsed["error"].lower() or "parent" in parsed["error"].lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_rejects_invalid_parent_uuid(
    session_factory, org_id: uuid.UUID, parent_session,
):
    result = await _spawn_task_call(
        {"goal": "g", "parents": ["not-a-uuid"]},
        parent_session, session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_deduplicates_parents(
    session_factory, org_id: uuid.UUID, parent_session,
):
    """Passing the same parent twice doesn't create a duplicate TaskLink."""
    async with session_factory() as db:
        parent_task = Task(
            org_id=org_id,
            parent_session_id=parent_session.id,
            goal="p",
            status="done",
        )
        db.add(parent_task)
        await db.commit()
        parent_task_id = parent_task.id

    redis = AsyncMock()
    redis.zadd = AsyncMock()
    result = await _spawn_task_call(
        {
            "goal": "child",
            "parents": [str(parent_task_id), str(parent_task_id)],
        },
        parent_session, session_factory, redis=redis,
    )
    parsed = json.loads(result)
    assert parsed["status"] == "running"  # one done parent → ready → spawn

    async with session_factory() as db:
        link_count = (await db.execute(
            select(TaskLink).where(TaskLink.child_id == uuid.UUID(parsed["task_id"]))
        )).scalars().all()
        assert len(link_count) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_persists_context_and_max_attempts(
    session_factory, org_id: uuid.UUID, parent_session,
):
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    result = await _spawn_task_call(
        {"goal": "g", "context": "user wants markdown output", "max_attempts": 5},
        parent_session, session_factory, redis=redis,
    )
    parsed = json.loads(result)
    async with session_factory() as db:
        task = await db.get(Task, uuid.UUID(parsed["task_id"]))
        assert task.context == "user wants markdown output"
        assert task.max_attempts == 5
