"""Functional worker spawning, messaging, stopping, and parent notification tests."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.config import SHARED_WORK_QUEUE_KEY, encode_queue_member
from surogates.harness.budget import IterationBudget
from surogates.session.events import EventType


def _default_workspace_config() -> dict:
    return {
        "storage_bucket": "tenant-bucket",
        "storage_key_prefix": "",
        "workspace_path": "/workspace/tenant-bucket/parent",
        "supports_vision": False,
    }


def _make_session(**overrides: Any) -> MagicMock:
    """Build a workspace-ready mock session.

    ``create_child_session`` requires the parent to carry storage_bucket,
    workspace_path, and supports_vision in its config.  Default to a
    valid set so individual tests don't need to repeat the boilerplate.
    """
    session = MagicMock()
    session.id = overrides.get("id", uuid4())
    session.parent_id = overrides.get("parent_id")
    session.agent_id = overrides.get("agent_id", "agent-test")
    session.model = overrides.get("model", "gpt-4o")
    session.config = overrides.get("config", _default_workspace_config())
    return session


def _make_store() -> AsyncMock:
    store = AsyncMock()
    child = _make_session(id=uuid4(), parent_id=uuid4())
    store.create_session = AsyncMock(return_value=child)
    store.emit_event = AsyncMock(return_value=1)
    store.get_session = AsyncMock(return_value=_make_session())
    store.get_events = AsyncMock(return_value=[])
    return store


def _make_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    redis.publish = AsyncMock()
    return redis


class TestWorkerNotification:
    @pytest.mark.asyncio
    async def test_notify_parent_on_completion(self) -> None:
        from surogates.harness.worker_notify import notify_parent_on_completion

        worker_id = uuid4()
        parent_id = uuid4()

        # Simulate worker events with a final LLM response.
        llm_event = MagicMock()
        llm_event.type = EventType.LLM_RESPONSE.value
        llm_event.data = {"message": {"content": "Auth bug fixed. Commit abc123."}}

        store = AsyncMock()
        store.get_events = AsyncMock(return_value=[llm_event])
        store.emit_event = AsyncMock(return_value=1)

        redis = _make_redis()
        org_id = uuid4()

        await notify_parent_on_completion(
            session_store=store,
            worker_session_id=worker_id,
            parent_session_id=parent_id,
            org_id=str(org_id),
            agent_id="agent-test",
            redis=redis,
        )

        # WORKER_COMPLETE emitted into parent session.
        emit_call = store.emit_event.call_args
        assert emit_call[0][0] == parent_id
        assert emit_call[0][1] == EventType.WORKER_COMPLETE
        assert "Auth bug fixed" in emit_call[0][2]["result"]
        assert emit_call[0][2]["worker_id"] == str(worker_id)

        # Parent re-enqueued on the shared work queue with its tenant tuple.
        redis.zadd.assert_called_once_with(
            SHARED_WORK_QUEUE_KEY,
            {
                encode_queue_member(
                    org_id=str(org_id),
                    agent_id="agent-test",
                    session_id=str(parent_id),
                ): 0,
            },
            lt=True,
        )

    @pytest.mark.asyncio
    async def test_notify_parent_on_failure(self) -> None:
        from surogates.harness.worker_notify import notify_parent_on_failure

        worker_id = uuid4()
        parent_id = uuid4()

        store = AsyncMock()
        store.emit_event = AsyncMock(return_value=1)
        redis = _make_redis()

        await notify_parent_on_failure(
            session_store=store,
            worker_session_id=worker_id,
            parent_session_id=parent_id,
            org_id=str(uuid4()),
            agent_id="agent-test",
            error="LLM call failed: 429 rate limited",
            redis=redis,
        )

        emit_call = store.emit_event.call_args
        assert emit_call[0][0] == parent_id
        assert emit_call[0][1] == EventType.WORKER_FAILED
        assert "429" in emit_call[0][2]["error"]

        redis.zadd.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_response_produces_fallback(self) -> None:
        from surogates.harness.worker_notify import notify_parent_on_completion

        store = AsyncMock()
        store.get_events = AsyncMock(return_value=[])  # no LLM events
        store.emit_event = AsyncMock(return_value=1)

        await notify_parent_on_completion(
            session_store=store,
            worker_session_id=uuid4(),
            parent_session_id=uuid4(),
            org_id=str(uuid4()),
            agent_id="agent-test",
        )

        result = store.emit_event.call_args[0][2]["result"]
        assert "no response" in result.lower()


class TestDelegateQueueFix:
    @pytest.mark.asyncio
    async def test_uses_work_queue_not_task_queue(self) -> None:
        from tests.test_delegate import _run_agent

        parent_id = uuid4()
        child_id = uuid4()

        store = _make_store()
        parent = _make_session(id=parent_id, agent_id="agent-1")
        child = _make_session(id=child_id, parent_id=parent_id)
        store.get_session = AsyncMock(return_value=parent)
        store.create_session = AsyncMock(return_value=child)

        # Make child "complete" immediately so the poll doesn't block.
        complete_event = MagicMock()
        complete_event.type = EventType.SESSION_COMPLETE.value
        complete_event.data = {"reason": "completed"}
        llm_event = MagicMock()
        llm_event.type = EventType.LLM_RESPONSE.value
        llm_event.data = {"message": {"content": "Done."}}
        store.get_events = AsyncMock(return_value=[llm_event, complete_event])

        redis = _make_redis()
        org_id = uuid4()

        await _run_agent(
            {"goal": "Test task"},
            session_store=store,
            redis=redis,
            tenant=MagicMock(user_id=uuid4(), org_id=org_id),
            session_id=str(parent_id),
            budget=IterationBudget(max_total=50),
        )

        # Must use zadd to the shared work_queue, NOT lpush to task_queue.
        redis.zadd.assert_called_once_with(
            SHARED_WORK_QUEUE_KEY,
            {
                encode_queue_member(
                    org_id=str(org_id),
                    agent_id="agent-1",
                    session_id=str(child_id),
                ): 0,
            },
            lt=True,
        )
        redis.lpush.assert_not_called()
