"""Integration tests for unblock_task, cancel_task, and worker_block tool handlers."""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from surogates.db.models import Session as ORMSession, Task
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
            id=pid,
            org_id=org_id,
            agent_id="orchestrator",
            channel="web",
            status="active",
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


# ---------------------------------------------------------------------------
# unblock_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_unblock_task_ready_with_appended_context(
    session_factory, org_id: uuid.UUID, parent_session,
):
    """Unblock flips status to ready, clears blocked_reason, appends context."""
    from surogates.tasks.tools import _unblock_task_handler

    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="blocked",
            context="original context",
            blocked_reason="need an answer",
        )
        db.add(t)
        await db.commit()
        tid = t.id

    redis = AsyncMock()
    result = await _unblock_task_handler(
        {"task_id": str(tid), "additional_context": "answer is X"},
        session_store=AsyncMock(),
        redis=redis,
        tenant=MagicMock(org_id=org_id),
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert parsed.get("ok") is True
    assert parsed["status"] == "ready"

    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.status == "ready"
        assert t.blocked_reason is None
        assert "original context" in t.context
        assert "answer is X" in t.context
        assert "[unblock at" in t.context


@pytest.mark.asyncio(loop_scope="session")
async def test_unblock_task_without_additional_context_preserves_existing(
    session_factory, org_id: uuid.UUID, parent_session,
):
    from surogates.tasks.tools import _unblock_task_handler

    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="blocked", context="original",
        )
        db.add(t)
        await db.commit()
        tid = t.id

    await _unblock_task_handler(
        {"task_id": str(tid)},
        session_store=AsyncMock(), redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )
    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.status == "ready"
        assert t.context == "original"


@pytest.mark.asyncio(loop_scope="session")
async def test_unblock_task_refuses_when_not_blocked(
    session_factory, org_id: uuid.UUID, parent_session,
):
    from surogates.tasks.tools import _unblock_task_handler

    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running",
        )
        db.add(t)
        await db.commit()
        tid = t.id

    result = await _unblock_task_handler(
        {"task_id": str(tid)},
        session_store=AsyncMock(), redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "blocked" in parsed["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_unblock_task_enforces_ownership(
    session_factory, org_id: uuid.UUID, parent_session,
):
    """Only the spawning parent session can unblock."""
    from surogates.tasks.tools import _unblock_task_handler

    other_session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=other_session_id, org_id=org_id, agent_id="orchestrator",
            channel="web", status="active",
        ))
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="blocked",
        )
        db.add(t)
        await db.commit()
        tid = t.id

    result = await _unblock_task_handler(
        {"task_id": str(tid)},
        session_store=AsyncMock(), redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(other_session_id),  # NOT the parent
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "spawn" in parsed["error"].lower() or "session" in parsed["error"].lower()


# ---------------------------------------------------------------------------
# cancel_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_task_non_running_sets_cancelled_no_interrupt(
    session_factory, org_id: uuid.UUID, parent_session,
):
    from surogates.tasks.tools import _cancel_task_handler

    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="todo",
        )
        db.add(t)
        await db.commit()
        tid = t.id

    redis = AsyncMock()
    redis.publish = AsyncMock()
    result = await _cancel_task_handler(
        {"task_id": str(tid)},
        session_store=AsyncMock(), redis=redis,
        tenant=MagicMock(org_id=org_id),
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert parsed.get("ok") is True
    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.status == "cancelled"
        assert t.completed_at is not None
    # No publish — was not running.
    redis.publish.assert_not_called()


@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_task_running_publishes_interrupt(
    session_factory, org_id: uuid.UUID, parent_session,
):
    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.tasks.tools import _cancel_task_handler

    worker_session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=worker_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active",
        ))
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running",
            current_session_id=worker_session_id, attempt_count=1,
        )
        db.add(t)
        await db.commit()
        tid = t.id

    redis = AsyncMock()
    redis.publish = AsyncMock()
    await _cancel_task_handler(
        {"task_id": str(tid)},
        session_store=AsyncMock(), redis=redis,
        tenant=MagicMock(org_id=org_id),
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )
    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.status == "cancelled"
    redis.publish.assert_called_once()
    channel = redis.publish.call_args[0][0]
    assert channel == f"{INTERRUPT_CHANNEL_PREFIX}{worker_session_id}"


@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_task_refuses_terminal_states(
    session_factory, org_id: uuid.UUID, parent_session,
):
    from surogates.tasks.tools import _cancel_task_handler

    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="done",
        )
        db.add(t)
        await db.commit()
        tid = t.id

    result = await _cancel_task_handler(
        {"task_id": str(tid)},
        session_store=AsyncMock(), redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "terminal" in parsed["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_task_enforces_ownership(
    session_factory, org_id: uuid.UUID, parent_session,
):
    from surogates.tasks.tools import _cancel_task_handler

    other_session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=other_session_id, org_id=org_id, agent_id="orchestrator",
            channel="web", status="active",
        ))
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="ready",
        )
        db.add(t)
        await db.commit()
        tid = t.id

    result = await _cancel_task_handler(
        {"task_id": str(tid)},
        session_store=AsyncMock(), redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(other_session_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed


# ---------------------------------------------------------------------------
# worker_block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_worker_block_marks_blocked_emits_event_publishes_interrupt(
    session_factory, org_id: uuid.UUID, parent_session, session_store,
):
    """worker_block sets task to blocked, emits TASK_BLOCKED to parent,
    publishes INTERRUPT to its own session."""
    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.tasks.tools import _worker_block_handler

    worker_session_id = uuid.uuid4()
    async with session_factory() as db:
        # Insert Task without current_session_id first (FK chicken-and-egg
        # with sessions.task_id: each row references the other).
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running", attempt_count=1,
        )
        db.add(t)
        await db.flush()
        db.add(ORMSession(
            id=worker_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active", task_id=t.id,
        ))
        await db.flush()
        t.current_session_id = worker_session_id
        await db.commit()
        tid = t.id

    redis = AsyncMock()
    redis.publish = AsyncMock()

    result = await _worker_block_handler(
        {"reason": "rate limit key unclear"},
        session_store=session_store, redis=redis,
        tenant=MagicMock(org_id=org_id),
        session_id=str(worker_session_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert parsed.get("ok") is True
    assert parsed["status"] == "blocked"

    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.status == "blocked"
        assert t.blocked_reason == "rate limit key unclear"

    # TASK_BLOCKED event on parent session.
    from surogates.db.models import Event
    async with session_factory() as db:
        events = (await db.execute(
            select(Event).where(
                Event.session_id == parent_session.id,
                Event.type == EventType.TASK_BLOCKED.value,
            )
        )).scalars().all()
        assert len(events) == 1
        data = events[0].data
        assert data["task_id"] == str(tid)
        assert data["worker_id"] == str(worker_session_id)
        assert data["reason"] == "rate limit key unclear"

    # INTERRUPT to its own session channel.
    redis.publish.assert_called_once()
    channel = redis.publish.call_args[0][0]
    assert channel == f"{INTERRUPT_CHANNEL_PREFIX}{worker_session_id}"


@pytest.mark.asyncio(loop_scope="session")
async def test_worker_block_refuses_when_session_has_no_task(
    session_factory, org_id: uuid.UUID, parent_session, session_store,
):
    """A plain (non-task) session calling worker_block gets an error."""
    from surogates.tasks.tools import _worker_block_handler

    # parent_session has task_id=None
    result = await _worker_block_handler(
        {"reason": "anything"},
        session_store=session_store, redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "task" in parsed["error"].lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_worker_block_refuses_when_attempt_was_reclaimed(
    session_factory, org_id: uuid.UUID, parent_session, session_store,
):
    """If task.current_session_id no longer matches the caller, refuse
    (e.g., the dispatcher reclaimed this attempt as stale)."""
    from surogates.tasks.tools import _worker_block_handler

    stale_session_id = uuid.uuid4()
    new_session_id = uuid.uuid4()
    async with session_factory() as db:
        # Insert both sessions before pointing the Task at one (FK ordering).
        db.add(ORMSession(
            id=new_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active",
        ))
        await db.flush()
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running",
            current_session_id=new_session_id,  # points at new attempt
            attempt_count=2,
        )
        db.add(t)
        await db.flush()
        db.add(ORMSession(
            id=stale_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active", task_id=t.id,
        ))
        await db.commit()

    result = await _worker_block_handler(
        {"reason": "I'm stale but trying"},
        session_store=session_store, redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(stale_session_id),  # stale attempt
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "current" in parsed["error"].lower() or "reclaim" in parsed["error"].lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_worker_block_does_not_increment_attempt_count(
    session_factory, org_id: uuid.UUID, parent_session, session_store,
):
    """Blocking is a deliberate pause, NOT a failure — attempt_count stays."""
    from surogates.tasks.tools import _worker_block_handler

    worker_session_id = uuid.uuid4()
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running", attempt_count=1,
        )
        db.add(t)
        await db.flush()
        db.add(ORMSession(
            id=worker_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active", task_id=t.id,
        ))
        await db.flush()
        t.current_session_id = worker_session_id
        await db.commit()
        tid = t.id

    redis = AsyncMock()
    redis.publish = AsyncMock()
    await _worker_block_handler(
        {"reason": "stop me"},
        session_store=session_store, redis=redis,
        tenant=MagicMock(org_id=org_id),
        session_id=str(worker_session_id),
        session_factory=session_factory,
    )

    async with session_factory() as db:
        t = await db.get(Task, tid)
        assert t.attempt_count == 1  # unchanged
