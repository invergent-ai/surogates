"""Unit tests for WORKER_COMPLETE / WORKER_FAILED payload extension to
include ``task_id`` when the worker session was running for a task."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from surogates.session.events import EventType


@pytest.mark.asyncio
async def test_worker_complete_payload_includes_task_id_when_set():
    """When ``task_id`` is passed, the WORKER_COMPLETE payload carries it."""
    from surogates.harness.worker_notify import notify_parent_on_completion

    parent_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = uuid.uuid4()

    store = AsyncMock()
    store.emit_event = AsyncMock(return_value=1)
    store.get_events = AsyncMock(return_value=[])  # extract_final_response → ""

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    await notify_parent_on_completion(
        session_store=store,
        worker_session_id=worker_id,
        parent_session_id=parent_id,
        agent_id="orchestrator",
        redis=redis,
        task_id=task_id,
    )

    emit_calls = store.emit_event.call_args_list
    complete_calls = [c for c in emit_calls if c[0][1] == EventType.WORKER_COMPLETE]
    assert len(complete_calls) == 1
    payload = complete_calls[0][0][2]
    assert payload["worker_id"] == str(worker_id)
    assert payload["task_id"] == str(task_id)


@pytest.mark.asyncio
async def test_worker_complete_payload_omits_task_id_when_none():
    """A plain spawn_worker session passes task_id=None → payload omits the key."""
    from surogates.harness.worker_notify import notify_parent_on_completion

    parent_id = uuid.uuid4()
    worker_id = uuid.uuid4()

    store = AsyncMock()
    store.emit_event = AsyncMock(return_value=1)
    store.get_events = AsyncMock(return_value=[])

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    await notify_parent_on_completion(
        session_store=store,
        worker_session_id=worker_id,
        parent_session_id=parent_id,
        agent_id="orchestrator",
        redis=redis,
        # task_id explicitly omitted (defaults to None)
    )

    emit_calls = store.emit_event.call_args_list
    payload = [c for c in emit_calls if c[0][1] == EventType.WORKER_COMPLETE][0][0][2]
    assert "task_id" not in payload


@pytest.mark.asyncio
async def test_worker_failed_payload_includes_task_id_when_set():
    from surogates.harness.worker_notify import notify_parent_on_failure

    parent_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    task_id = uuid.uuid4()

    store = AsyncMock()
    store.emit_event = AsyncMock(return_value=1)
    redis = AsyncMock()
    redis.zadd = AsyncMock()

    await notify_parent_on_failure(
        session_store=store,
        worker_session_id=worker_id,
        parent_session_id=parent_id,
        agent_id="orchestrator",
        error="boom",
        redis=redis,
        task_id=task_id,
    )

    emit_calls = store.emit_event.call_args_list
    failed_calls = [c for c in emit_calls if c[0][1] == EventType.WORKER_FAILED]
    assert len(failed_calls) == 1
    payload = failed_calls[0][0][2]
    assert payload["worker_id"] == str(worker_id)
    assert payload["task_id"] == str(task_id)
    assert payload["error"] == "boom"


@pytest.mark.asyncio
async def test_worker_failed_payload_omits_task_id_when_none():
    from surogates.harness.worker_notify import notify_parent_on_failure

    parent_id = uuid.uuid4()
    worker_id = uuid.uuid4()

    store = AsyncMock()
    store.emit_event = AsyncMock(return_value=1)
    redis = AsyncMock()
    redis.zadd = AsyncMock()

    await notify_parent_on_failure(
        session_store=store,
        worker_session_id=worker_id,
        parent_session_id=parent_id,
        agent_id="orchestrator",
        error="boom",
        redis=redis,
    )

    emit_calls = store.emit_event.call_args_list
    payload = [c for c in emit_calls if c[0][1] == EventType.WORKER_FAILED][0][0][2]
    assert "task_id" not in payload
