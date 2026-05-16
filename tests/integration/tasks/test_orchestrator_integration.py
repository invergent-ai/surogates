"""End-to-end integration tests for the task layer wired into the Orchestrator.

Verifies:
* The Orchestrator constructor accepts ``session_factory`` and
  ``tenant_for_task`` kwargs.
* A full fan-in flow: two parents → done → synthesizer auto-promotes
  via the tick → tick claims and spawns → parent completion event.
* Crash + retry: a worker session that fails without WORKER_COMPLETE
  is retried by the next tick within ``max_attempts``.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from surogates.db.models import Event, Session as ORMSession, Task, TaskLink
from surogates.session.events import EventType

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
                "workspace_path": f"/workspace/test/{pid}",
                "supports_vision": False,
            },
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


def test_orchestrator_accepts_task_layer_kwargs():
    """Orchestrator.__init__ takes session_factory + tenant_for_task without breaking."""
    from surogates.orchestrator.dispatcher import Orchestrator

    o = Orchestrator(
        redis_client=AsyncMock(),
        session_store=AsyncMock(),
        harness_factory=lambda *a, **kw: None,
        agent_id="orchestrator",
        queue_key="surogates:work_queue:orchestrator",
        session_factory=MagicMock(),
        tenant_for_task=lambda task: MagicMock(org_id=task.org_id),
    )
    assert o._session_factory is not None
    assert o._tenant_for_task is not None


def test_orchestrator_works_without_task_layer_kwargs():
    """Construction without the new kwargs keeps the old contract working."""
    from surogates.orchestrator.dispatcher import Orchestrator

    o = Orchestrator(
        redis_client=AsyncMock(),
        session_store=AsyncMock(),
        harness_factory=lambda *a, **kw: None,
        agent_id="orchestrator",
        queue_key="surogates:work_queue:orchestrator",
    )
    assert o._session_factory is None
    assert o._tenant_for_task is None


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_fan_in_promote_and_enqueue(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """Two parents complete → synthesizer auto-promotes → tick spawns it."""
    from surogates.tasks.dispatcher import tasks_tick

    async with session_factory() as db:
        p1 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="p1", status="done")
        p2 = Task(org_id=org_id, parent_session_id=parent_session.id, goal="p2", status="done")
        synth = Task(org_id=org_id, parent_session_id=parent_session.id, goal="synth", status="todo")
        db.add_all([p1, p2, synth])
        await db.flush()
        db.add_all([
            TaskLink(parent_id=p1.id, child_id=synth.id),
            TaskLink(parent_id=p2.id, child_id=synth.id),
        ])
        await db.commit()
        synth_id = synth.id

    redis = AsyncMock()
    redis.zadd = AsyncMock()
    redis.publish = AsyncMock()

    result = await tasks_tick(
        session_factory=session_factory,
        redis=redis,
        session_store=session_store,
        tenant_for_task=lambda task: MagicMock(org_id=task.org_id),
    )
    assert result["promoted"] >= 1
    assert result["enqueued"] >= 1

    async with session_factory() as db:
        s = await db.get(Task, synth_id)
        assert s.status == "running"
        assert s.current_session_id is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_crash_and_retry_within_max_attempts(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """A task that crashes once retries on the next tick."""
    from surogates.tasks.dispatcher import tasks_tick

    worker_id_1 = uuid.uuid4()
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running",
            attempt_count=1, max_attempts=3,
        )
        db.add(t)
        await db.flush()
        db.add(ORMSession(
            id=worker_id_1, org_id=org_id, agent_id="orchestrator",
            channel="task", status="failed", task_id=t.id,
            config={
                "storage_bucket": "test-bucket",
                "workspace_path": f"/workspace/test/{worker_id_1}",
                "supports_vision": False,
            },
        ))
        await db.flush()
        t.current_session_id = worker_id_1
        await db.commit()
        tid = t.id

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    # First tick: finalize sees crashed session, sets back to ready,
    # then claims + spawns a new attempt.
    await tasks_tick(
        session_factory=session_factory,
        redis=redis,
        session_store=session_store,
        tenant_for_task=lambda task: MagicMock(org_id=task.org_id),
    )

    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.status == "running"
        assert t.attempt_count == 2  # was 1, finalize set 'ready', enqueue bumped to 2
        assert t.current_session_id != worker_id_1  # new attempt session


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_completion_finalizes_via_tick(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """Worker session completes with WORKER_COMPLETE → tick writes done + result."""
    from surogates.tasks.dispatcher import tasks_tick

    worker_id = uuid.uuid4()
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="research", status="running", attempt_count=1,
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

    # Simulate the harness emitting WORKER_COMPLETE and updating session.status.
    await session_store.emit_event(
        worker_id, EventType.WORKER_COMPLETE,
        {"worker_id": str(worker_id), "result": "found 3 sources"},
    )
    async with session_factory() as db:
        sess = await db.get(ORMSession, worker_id)
        sess.status = "completed"
        await db.commit()

    await tasks_tick(
        session_factory=session_factory,
        redis=AsyncMock(zadd=AsyncMock()),
        session_store=session_store,
        tenant_for_task=lambda task: MagicMock(org_id=task.org_id),
    )

    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.status == "done"
        assert t.result == "found 3 sources"
        assert t.completed_at is not None
