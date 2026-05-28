"""Integration tests for ``tasks_tick`` (promote, finalize, enqueue)."""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from surogates.db.models import Event, Session as ORMSession, Task, TaskLink
from surogates.session.events import EventType
from surogates.tasks.dispatcher import (
    _finalize_ended_sessions,
    _promote_todo_to_ready,
    _enqueue_ready_tasks,
    tasks_tick,
)

from tests.integration.conftest import create_org


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def parent_session(session_factory, org_id: uuid.UUID) -> ORMSession:
    pid = uuid.uuid4()
    async with session_factory() as db:
        s = ORMSession(
            id=pid, org_id=org_id, agent_id="orchestrator",
            channel="web", status="active",
            config={
                "storage_bucket": "test-bucket",
                "storage_key_prefix": "",
                "workspace_path": f"/workspace/test/{pid}",
                "supports_vision": False,
            },
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Promote: todo -> ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_promote_promotes_when_all_parents_done(
    session_factory, org_id: uuid.UUID, parent_session,
):
    """Two done parents → synthesizer auto-promotes from todo to ready."""
    async with session_factory() as db:
        p1 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="p1", status="done")
        p2 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="p2", status="done")
        c = Task(org_id=org_id, parent_session_id=parent_session.id, goal="c", status="todo")
        db.add_all([p1, p2, c])
        await db.flush()
        db.add_all([
            TaskLink(parent_id=p1.id, child_id=c.id),
            TaskLink(parent_id=p2.id, child_id=c.id),
        ])
        await db.commit()
        cid = c.id

    async with session_factory() as db:
        promoted = await _promote_todo_to_ready(db)
        await db.commit()
    assert promoted >= 1

    async with session_factory() as db:
        c = await db.get(Task, cid)
        assert c.status == "ready"


@pytest.mark.asyncio(loop_scope="session")
async def test_promote_skips_when_any_parent_unfinished(
    session_factory, org_id: uuid.UUID, parent_session,
):
    async with session_factory() as db:
        p1 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="p1", status="done")
        p2 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="p2", status="running")
        c = Task(org_id=org_id, parent_session_id=parent_session.id, goal="c", status="todo")
        db.add_all([p1, p2, c])
        await db.flush()
        db.add_all([
            TaskLink(parent_id=p1.id, child_id=c.id),
            TaskLink(parent_id=p2.id, child_id=c.id),
        ])
        await db.commit()
        cid = c.id

    async with session_factory() as db:
        await _promote_todo_to_ready(db)
        await db.commit()
        c = await db.get(Task, cid)
        assert c.status == "todo"


@pytest.mark.asyncio(loop_scope="session")
async def test_promote_does_not_unblock_on_cancelled_or_failed_parent(
    session_factory, org_id: uuid.UUID, parent_session,
):
    """Cancelled / failed parents must NOT promote children — design rule."""
    async with session_factory() as db:
        p1 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="p1", status="cancelled")
        p2 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="p2", status="failed")
        c1 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="c1", status="todo")
        c2 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="c2", status="todo")
        db.add_all([p1, p2, c1, c2])
        await db.flush()
        db.add_all([
            TaskLink(parent_id=p1.id, child_id=c1.id),
            TaskLink(parent_id=p2.id, child_id=c2.id),
        ])
        await db.commit()
        c1_id = c1.id
        c2_id = c2.id

    async with session_factory() as db:
        await _promote_todo_to_ready(db)
        await db.commit()
        c1 = await db.get(Task, c1_id)
        c2 = await db.get(Task, c2_id)
        assert c1.status == "todo"
        assert c2.status == "todo"


# ---------------------------------------------------------------------------
# Finalize: running -> done / ready / failed
# ---------------------------------------------------------------------------


async def _set_session_status(session_factory, session_id: uuid.UUID, status: str):
    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        sess.status = status
        await db.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_finalize_completed_session_marks_task_done_with_result(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    worker_id = uuid.uuid4()
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running", attempt_count=1,
        )
        db.add(t)
        await db.flush()
        db.add(ORMSession(
            id=worker_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active", task_id=t.id,
            config={
                "storage_bucket": "test-bucket",
                "workspace_path": f"/workspace/test/{worker_id}",
                "supports_vision": False,
            },
        ))
        await db.flush()
        t.current_session_id = worker_id
        await db.commit()
        tid = t.id

    # Emit WORKER_COMPLETE event on the worker session (the real harness
    # would do this; we simulate it).
    await session_store.emit_event(
        worker_id, EventType.WORKER_COMPLETE,
        {"worker_id": str(worker_id), "result": "great success"},
    )
    await _set_session_status(session_factory, worker_id, "completed")

    async with session_factory() as db:
        finalized = await _finalize_ended_sessions(db, session_store=session_store)
    assert finalized == 1

    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.status == "done"
        assert t.result == "great success"
        assert t.completed_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_finalize_crashed_with_attempts_remaining_retries(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    worker_id = uuid.uuid4()
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running",
            attempt_count=1, max_attempts=3,
        )
        db.add(t)
        await db.flush()
        db.add(ORMSession(
            id=worker_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="failed", task_id=t.id,
        ))
        await db.flush()
        t.current_session_id = worker_id
        await db.commit()
        tid = t.id

    # No WORKER_COMPLETE event — finalize classifies as crashed.
    async with session_factory() as db:
        await _finalize_ended_sessions(db, session_store=session_store)
        t = await db.get(Task, tid)
        assert t.status == "ready"  # retry
        # attempt_count is NOT bumped on finalize — it was bumped at claim.
        assert t.attempt_count == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_finalize_crashed_after_max_attempts_marks_failed_and_emits_event(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    worker_id = uuid.uuid4()
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running",
            attempt_count=3, max_attempts=3,
        )
        db.add(t)
        await db.flush()
        db.add(ORMSession(
            id=worker_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="failed", task_id=t.id,
        ))
        await db.flush()
        t.current_session_id = worker_id
        await db.commit()
        tid = t.id

    async with session_factory() as db:
        await _finalize_ended_sessions(db, session_store=session_store)
        t = await db.get(Task, tid)
        assert t.status == "failed"
        assert t.completed_at is not None

    # TASK_FAILED event on parent.
    async with session_factory() as db:
        events = (await db.execute(
            select(Event).where(
                Event.session_id == parent_session.id,
                Event.type == EventType.TASK_FAILED.value,
            )
        )).scalars().all()
        assert len(events) == 1
        data = events[0].data
        assert data["task_id"] == str(tid)
        assert data["attempt_count"] == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_finalize_skips_running_sessions(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """Tasks whose Session is still ``active`` are not touched."""
    worker_id = uuid.uuid4()
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running", attempt_count=1,
        )
        db.add(t)
        await db.flush()
        db.add(ORMSession(
            id=worker_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active", task_id=t.id,
        ))
        await db.flush()
        t.current_session_id = worker_id
        await db.commit()
        tid = t.id

    async with session_factory() as db:
        finalized = await _finalize_ended_sessions(db, session_store=session_store)
        assert finalized == 0
        t = await db.get(Task, tid)
        assert t.status == "running"


# ---------------------------------------------------------------------------
# Enqueue: ready -> running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_enqueue_claims_ready_task_and_spawns(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    # The integration test session_factory is session-scoped, so leftover
    # rows from prior tests may exist. We assert on THIS task's outcome
    # rather than a global enqueued count.
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="research", status="ready",
        )
        db.add(t)
        await db.commit()
        tid = t.id

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    def tenant_for_task(task):
        return MagicMock(org_id=task.org_id)

    enqueued = await _enqueue_ready_tasks(
        session_factory=session_factory,
        redis=redis,
        session_store=session_store,
        tenant_for_task=tenant_for_task,
    )
    # Our task must have been picked up alongside any leftovers.
    assert enqueued >= 1

    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.status == "running"
        assert t.current_session_id is not None
        assert t.attempt_count == 1
        assert t.started_at is not None

    # zadd called at least once (for our task; may be more for leftovers).
    assert redis.zadd.await_count >= 1


@pytest.mark.asyncio(loop_scope="session")
async def test_enqueue_respects_per_tick_cap(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """If more than _MAX_ENQUEUES_PER_TICK ready tasks exist, only the cap is enqueued."""
    from sqlalchemy import delete, text

    from surogates.tasks.dispatcher import _MAX_ENQUEUES_PER_TICK

    # Integration tests share a session-scoped DB. Prior tests may have
    # left ``ready`` Tasks pointing at parent sessions without the
    # required workspace config; those would fail to spawn here and
    # silently lower our enqueue count below the cap. Clear the slate
    # so our 13 tasks are the only candidates the dispatcher sees.
    async with session_factory() as db:
        await db.execute(text(
            "DELETE FROM task_links "
            "WHERE parent_id IN (SELECT id FROM tasks WHERE status = 'ready') "
            "OR child_id IN (SELECT id FROM tasks WHERE status = 'ready')"
        ))
        await db.execute(delete(Task).where(Task.status == "ready"))
        await db.commit()

    n = _MAX_ENQUEUES_PER_TICK + 3
    async with session_factory() as db:
        for i in range(n):
            db.add(Task(
                org_id=org_id, parent_session_id=parent_session.id,
                goal=f"g{i}", status="ready",
            ))
        await db.commit()

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    enqueued = await _enqueue_ready_tasks(
        session_factory=session_factory,
        redis=redis,
        session_store=session_store,
        tenant_for_task=lambda task: MagicMock(org_id=task.org_id),
    )
    assert enqueued == _MAX_ENQUEUES_PER_TICK


# ---------------------------------------------------------------------------
# tasks_tick: full pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_tasks_tick_full_pass_promote_and_enqueue(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """End-to-end: parent done → child promoted by tick step 1 → claimed by step 3."""
    async with session_factory() as db:
        parent = Task(org_id=org_id, parent_session_id=parent_session.id, goal="p", status="done")
        child = Task(org_id=org_id, parent_session_id=parent_session.id, goal="c", status="todo")
        db.add_all([parent, child])
        await db.flush()
        db.add(TaskLink(parent_id=parent.id, child_id=child.id))
        await db.commit()
        child_id = child.id

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    result = await tasks_tick(
        session_factory=session_factory,
        redis=redis,
        session_store=session_store,
        tenant_for_task=lambda task: MagicMock(org_id=task.org_id),
    )
    assert result["promoted"] >= 1
    assert result["enqueued"] >= 1

    async with session_factory() as db:
        t = await db.get(Task, child_id)
        assert t.status == "running"
        assert t.current_session_id is not None
