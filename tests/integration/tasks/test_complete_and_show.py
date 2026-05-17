"""Integration tests for the worker-observability v1.5 tools.

Covers ``task_complete``, ``task_show``, retry-context injection into
the next attempt's USER_MESSAGE, and the ``notify_parent_on_completion``
override that surfaces explicit summary + metadata to the parent.
"""
from __future__ import annotations

import json
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
                "storage_key_prefix": "",
                "workspace_path": f"/workspace/test/{pid}",
                "supports_vision": False,
            },
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


async def _make_running_task(
    session_factory, org_id, parent_session, *,
    goal: str = "g", attempt_count: int = 1, context: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Task in 'running' status with a fresh worker Session.

    Returns ``(task_id, worker_session_id)``. Handles the chicken-and-egg
    FK between Task.current_session_id and Session.task_id with a
    two-flush sequence (insert task w/o current_session_id → insert
    session referencing task → set current_session_id → commit).
    """
    worker_id = uuid.uuid4()
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal=goal, context=context,
            status="running", attempt_count=attempt_count,
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
        return t.id, worker_id


# ---------------------------------------------------------------------------
# task_complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_task_complete_sets_done_with_summary_and_metadata(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    from surogates.tasks.tools import _task_complete_handler

    task_id, worker_session_id = await _make_running_task(
        session_factory, org_id, parent_session,
    )

    redis = AsyncMock()
    redis.publish = AsyncMock()

    result = await _task_complete_handler(
        {
            "summary": "shipped rate limiter",
            "metadata": {
                "changed_files": ["a.py", "b.py"],
                "tests_run": 14,
                "tests_passed": 14,
            },
        },
        session_store=session_store, redis=redis,
        tenant=MagicMock(org_id=org_id),
        session_id=str(worker_session_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["status"] == "done"

    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.status == "done"
        assert t.result == "shipped rate limiter"
        assert t.result_metadata["tests_run"] == 14
        assert t.result_metadata["changed_files"] == ["a.py", "b.py"]
        assert t.completed_at is not None

    redis.publish.assert_called_once()
    assert str(worker_session_id) in redis.publish.call_args[0][0]


@pytest.mark.asyncio(loop_scope="session")
async def test_task_complete_without_metadata_works(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    from surogates.tasks.tools import _task_complete_handler

    task_id, worker_session_id = await _make_running_task(
        session_factory, org_id, parent_session,
    )

    result = await _task_complete_handler(
        {"summary": "done"},
        session_store=session_store, redis=AsyncMock(publish=AsyncMock()),
        tenant=MagicMock(org_id=org_id),
        session_id=str(worker_session_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert parsed["ok"] is True

    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.result == "done"
        assert t.result_metadata is None


@pytest.mark.asyncio(loop_scope="session")
async def test_task_complete_refuses_when_session_has_no_task(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """A plain session calling task_complete gets an error."""
    from surogates.tasks.tools import _task_complete_handler

    result = await _task_complete_handler(
        {"summary": "anyway"},
        session_store=session_store, redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed


@pytest.mark.asyncio(loop_scope="session")
async def test_task_complete_refuses_stale_attempt(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """A stale attempt whose claim was reclaimed cannot complete the new attempt's task."""
    from surogates.tasks.tools import _task_complete_handler

    stale_session_id = uuid.uuid4()
    new_session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=new_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active",
        ))
        await db.flush()
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="g", status="running",
            current_session_id=new_session_id, attempt_count=2,
        )
        db.add(t)
        await db.flush()
        db.add(ORMSession(
            id=stale_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active", task_id=t.id,
        ))
        await db.commit()

    result = await _task_complete_handler(
        {"summary": "stale attempt"},
        session_store=session_store, redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(stale_session_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "current" in parsed["error"] or "reclaim" in parsed["error"]


# ---------------------------------------------------------------------------
# task_show
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_task_show_returns_task_parents_and_prior_attempts(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    from surogates.tasks.tools import _task_show_handler

    # Build: 2 done parents + a synthesizer that's on its 2nd attempt.
    async with session_factory() as db:
        p1 = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="research cost", status="done", result="vLLM cheaper",
            result_metadata={"sources": 5},
        )
        p2 = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="research perf", status="done", result="vLLM faster",
        )
        synth = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="synthesize", status="running", attempt_count=2,
        )
        db.add_all([p1, p2, synth])
        await db.flush()
        db.add_all([
            TaskLink(parent_id=p1.id, child_id=synth.id),
            TaskLink(parent_id=p2.id, child_id=synth.id),
        ])

        # Prior attempt session: had a WORKER_COMPLETE event.
        prior_session_id = uuid.uuid4()
        db.add(ORMSession(
            id=prior_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="failed", task_id=synth.id,
        ))
        # Current attempt session: still active.
        current_session_id = uuid.uuid4()
        db.add(ORMSession(
            id=current_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="active", task_id=synth.id,
        ))
        await db.flush()
        synth.current_session_id = current_session_id
        await db.commit()
        synth_id = synth.id
        p1_id = p1.id
        p2_id = p2.id

    # Simulate the prior attempt having emitted a TASK_BLOCKED event.
    await session_store.emit_event(
        prior_session_id, EventType.TASK_BLOCKED,
        {"task_id": str(synth_id), "reason": "ambiguous metric definition"},
    )

    result = await _task_show_handler(
        {},
        session_store=session_store, redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(current_session_id),
        session_factory=session_factory,
    )
    payload = json.loads(result)

    assert payload["task"]["id"] == str(synth_id)
    assert payload["task"]["goal"] == "synthesize"
    assert payload["task"]["attempt_count"] == 2

    assert len(payload["parents"]) == 2
    parent_ids = {p["id"] for p in payload["parents"]}
    assert parent_ids == {str(p1_id), str(p2_id)}
    p1_entry = next(p for p in payload["parents"] if p["id"] == str(p1_id))
    assert p1_entry["result"] == "vLLM cheaper"
    assert p1_entry["result_metadata"]["sources"] == 5

    assert len(payload["prior_attempts"]) == 1
    prior = payload["prior_attempts"][0]
    assert prior["outcome"] == "blocked"
    assert prior["blocked_reason"] == "ambiguous metric definition"


@pytest.mark.asyncio(loop_scope="session")
async def test_task_show_with_no_parents_or_prior(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """First attempt of a parentless task returns empty parents/prior lists."""
    from surogates.tasks.tools import _task_show_handler

    task_id, worker_id = await _make_running_task(
        session_factory, org_id, parent_session, goal="solo",
    )

    result = await _task_show_handler(
        {},
        session_store=session_store, redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(worker_id),
        session_factory=session_factory,
    )
    payload = json.loads(result)
    assert payload["task"]["goal"] == "solo"
    assert payload["parents"] == []
    assert payload["prior_attempts"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_task_show_refuses_when_session_has_no_task(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    from surogates.tasks.tools import _task_show_handler

    result = await _task_show_handler(
        {},
        session_store=session_store, redis=AsyncMock(),
        tenant=MagicMock(org_id=org_id),
        session_id=str(parent_session.id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed


# ---------------------------------------------------------------------------
# Retry context injection into _create_session_for_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_retry_attempt_user_message_includes_prior_attempts(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """An attempt with attempt_count > 1 gets a '## Prior attempts' section."""
    from surogates.tasks.spawn import _create_session_for_task

    # Build: Task with one failed prior session.
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="retry me", status="ready",
            attempt_count=1,  # next attempt will be #2
        )
        db.add(t)
        await db.flush()
        prior_session_id = uuid.uuid4()
        db.add(ORMSession(
            id=prior_session_id, org_id=org_id, agent_id="orchestrator",
            channel="task", status="failed", task_id=t.id,
        ))
        await db.commit()
        task_id = t.id

    # Simulate prior session emitting a WORKER_COMPLETE that the
    # finalize step would normally have used (but we're not running it
    # here — we're testing _create_session_for_task directly).
    await session_store.emit_event(
        prior_session_id, EventType.WORKER_COMPLETE,
        {"worker_id": str(prior_session_id), "result": "partial — found 1 source"},
    )

    # Now simulate the dispatcher claiming this task for retry: bump
    # attempt_count to 2, then call _create_session_for_task.
    async with session_factory() as db:
        task = await db.get(Task, task_id)
        task.attempt_count = 2
        task.current_session_id = None  # will be set by tick after spawn
        await db.commit()
        # Re-fetch a fresh row for the spawn.
        task = await db.get(Task, task_id)

    await _create_session_for_task(
        task,
        session_store=session_store,
        session_factory=session_factory,
        tenant=MagicMock(org_id=org_id),
    )

    # The most recently created session is the retry attempt.
    async with session_factory() as db:
        retry_session = (await db.execute(
            select(ORMSession)
            .where(ORMSession.task_id == task_id)
            .where(ORMSession.id != prior_session_id)
            .order_by(ORMSession.created_at.desc())
            .limit(1)
        )).scalar_one()

    # Inspect USER_MESSAGE on the retry session.
    events = await session_store.get_events(retry_session.id)
    user_msgs = [e for e in events if e.type == EventType.USER_MESSAGE.value]
    assert len(user_msgs) == 1
    content = user_msgs[0].data["content"]
    assert "retry me" in content
    assert "## Prior attempts on this task" in content
    assert "Attempt 1 (completed)" in content
    assert "partial — found 1 source" in content


@pytest.mark.asyncio(loop_scope="session")
async def test_first_attempt_user_message_omits_prior_attempts_section(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """First attempt (attempt_count <= 1 at spawn time) has no Prior Attempts header."""
    from surogates.tasks.spawn import _create_session_for_task

    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent_session.id,
            goal="fresh start", status="ready",
            attempt_count=1,  # this IS the first attempt (gets bumped on claim)
        )
        db.add(t)
        await db.commit()
        task = await db.get(Task, t.id)

    await _create_session_for_task(
        task,
        session_store=session_store,
        session_factory=session_factory,
        tenant=MagicMock(org_id=org_id),
    )

    async with session_factory() as db:
        sess = (await db.execute(
            select(ORMSession).where(ORMSession.task_id == task.id)
        )).scalar_one()

    events = await session_store.get_events(sess.id)
    user_msgs = [e for e in events if e.type == EventType.USER_MESSAGE.value]
    assert "## Prior attempts" not in user_msgs[0].data["content"]


# ---------------------------------------------------------------------------
# notify_parent_on_completion override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_notify_parent_uses_task_result_when_set(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """When task.result is set (worker called task_complete), the parent
    sees the explicit summary, not the LLM's last response."""
    from surogates.harness.worker_notify import notify_parent_on_completion

    task_id, worker_id = await _make_running_task(
        session_factory, org_id, parent_session,
    )
    # Simulate worker called task_complete: task fields are set.
    async with session_factory() as db:
        t = await db.get(Task, task_id)
        t.status = "done"
        t.result = "explicit summary from tool"
        t.result_metadata = {"changed_files": ["x.py"]}
        await db.commit()

    # Emit an LLM_RESPONSE event on the worker so the auto-extract has
    # something to find (which we then override).
    await session_store.emit_event(
        worker_id, EventType.LLM_RESPONSE,
        {"message": {"content": "raw LLM blather"}},
    )

    redis = AsyncMock()
    redis.zadd = AsyncMock()
    await notify_parent_on_completion(
        session_store=session_store,
        worker_session_id=worker_id,
        parent_session_id=parent_session.id,
        agent_id="orchestrator",
        redis=redis,
        task_id=task_id,
        session_factory=session_factory,
    )

    async with session_factory() as db:
        events = (await db.execute(
            select(Event).where(
                Event.session_id == parent_session.id,
                Event.type == EventType.WORKER_COMPLETE.value,
            )
        )).scalars().all()
        assert len(events) == 1
        data = events[0].data
        assert data["result"] == "explicit summary from tool"
        assert data["metadata"]["changed_files"] == ["x.py"]
        assert data["task_id"] == str(task_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_notify_parent_falls_back_to_llm_response_when_no_explicit_result(
    session_factory, session_store, org_id: uuid.UUID, parent_session,
):
    """A plain worker (no task_complete) gets the LLM's last response in WORKER_COMPLETE."""
    from surogates.harness.worker_notify import notify_parent_on_completion

    task_id, worker_id = await _make_running_task(
        session_factory, org_id, parent_session,
    )
    # task.result remains None (plain natural completion path).

    await session_store.emit_event(
        worker_id, EventType.LLM_RESPONSE,
        {"message": {"content": "the LLM's actual final words"}},
    )

    redis = AsyncMock()
    redis.zadd = AsyncMock()
    await notify_parent_on_completion(
        session_store=session_store,
        worker_session_id=worker_id,
        parent_session_id=parent_session.id,
        agent_id="orchestrator",
        redis=redis,
        task_id=task_id,
        session_factory=session_factory,
    )

    async with session_factory() as db:
        events = (await db.execute(
            select(Event).where(
                Event.session_id == parent_session.id,
                Event.type == EventType.WORKER_COMPLETE.value,
            )
        )).scalars().all()
        # Just check the most recent event (prior test may have added one).
        data = events[-1].data
        assert "the LLM's actual final words" in data["result"]
        # No metadata key because task.result_metadata is None.
        assert "metadata" not in data
