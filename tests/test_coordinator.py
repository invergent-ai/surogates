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


class TestSpawnWorker:
    @pytest.mark.asyncio
    async def test_spawns_child_session(self) -> None:
        from surogates.tools.builtin.coordinator import _spawn_worker_handler

        parent_id = uuid4()
        child_id = uuid4()
        parent = _make_session(id=parent_id, agent_id="agent-1")

        store = _make_store()
        child = _make_session(id=child_id, parent_id=parent_id)
        store.create_session = AsyncMock(return_value=child)
        store.get_session = AsyncMock(return_value=parent)

        redis = _make_redis()
        budget = IterationBudget(max_total=50)
        org_id = uuid4()

        result = await _spawn_worker_handler(
            {"goal": "Fix the auth bug"},
            session_store=store,
            redis=redis,
            tenant=MagicMock(user_id=uuid4(), org_id=org_id),
            session_id=str(parent_id),
            budget=budget,
        )

        parsed = json.loads(result)
        assert parsed["status"] == "spawned"
        assert parsed["worker_id"] == str(child_id)

        # Child session created with correct parent_id.
        store.create_session.assert_called_once()
        call_kwargs = store.create_session.call_args[1]
        assert call_kwargs["parent_id"] == parent_id
        assert call_kwargs["channel"] == "worker"

        # USER_MESSAGE emitted into child session.
        emit_calls = store.emit_event.call_args_list
        user_msg_calls = [c for c in emit_calls if c[0][1] == EventType.USER_MESSAGE]
        assert len(user_msg_calls) >= 1
        assert user_msg_calls[0][0][0] == child_id

        # WORKER_SPAWNED emitted into parent session.
        spawn_calls = [c for c in emit_calls if c[0][1] == EventType.WORKER_SPAWNED]
        assert len(spawn_calls) == 1
        assert spawn_calls[0][0][0] == parent_id

        # Enqueued to the shared work queue with the encoded tenant tuple.
        redis.zadd.assert_called_once_with(
            SHARED_WORK_QUEUE_KEY,
            {
                encode_queue_member(
                    org_id=str(org_id),
                    agent_id="agent-1",
                    session_id=str(child_id),
                ): 0,
            },
        )

    @pytest.mark.asyncio
    async def test_returns_immediately(self) -> None:
        """spawn_worker should not block — it returns the worker ID immediately."""
        from surogates.tools.builtin.coordinator import _spawn_worker_handler

        store = _make_store()
        result = await _spawn_worker_handler(
            {"goal": "Research the codebase"},
            session_store=store,
            redis=_make_redis(),
            tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
            session_id=str(uuid4()),
            budget=IterationBudget(max_total=50),
        )

        parsed = json.loads(result)
        assert parsed["status"] == "spawned"
        # No polling or waiting — just spawned and returned.

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_error(self) -> None:
        from surogates.tools.builtin.coordinator import _spawn_worker_handler

        budget = IterationBudget(max_total=1)
        budget.consume()  # exhaust it

        result = await _spawn_worker_handler(
            {"goal": "Do something"},
            session_store=_make_store(),
            redis=_make_redis(),
            tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
            session_id=str(uuid4()),
            budget=budget,
        )

        parsed = json.loads(result)
        assert "error" in parsed
        assert "budget" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_tool_whitelist_passed_to_config(self) -> None:
        from surogates.tools.builtin.coordinator import _spawn_worker_handler

        store = _make_store()

        await _spawn_worker_handler(
            {"goal": "Write tests", "tools": ["terminal", "read_file", "write_file"]},
            session_store=store,
            redis=_make_redis(),
            tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
            session_id=str(uuid4()),
            budget=IterationBudget(max_total=50),
        )

        config = store.create_session.call_args[1]["config"]
        assert "allowed_tools" in config
        assert "terminal" in config["allowed_tools"]
        # Coordinator tools must be stripped from whitelist.
        assert "spawn_worker" not in config["allowed_tools"]

    @pytest.mark.asyncio
    async def test_goal_required(self) -> None:
        from surogates.tools.builtin.coordinator import _spawn_worker_handler

        result = await _spawn_worker_handler(
            {"goal": ""},
            session_store=_make_store(),
            redis=_make_redis(),
            tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
            session_id=str(uuid4()),
            budget=IterationBudget(max_total=50),
        )

        parsed = json.loads(result)
        assert "error" in parsed


class TestSendWorkerMessage:
    @pytest.mark.asyncio
    async def test_sends_message_and_enqueues(self) -> None:
        from surogates.tools.builtin.coordinator import _send_worker_message_handler

        parent_id = uuid4()
        worker_id = uuid4()

        store = _make_store()
        worker = _make_session(id=worker_id, parent_id=parent_id)
        org_id = uuid4()
        worker.org_id = org_id
        store.get_session = AsyncMock(return_value=worker)

        redis = _make_redis()

        result = await _send_worker_message_handler(
            {"worker_id": str(worker_id), "message": "Fix the tests too"},
            session_store=store,
            redis=redis,
            session_id=str(parent_id),
        )

        parsed = json.loads(result)
        assert parsed["status"] == "sent"

        # USER_MESSAGE emitted into worker session.
        emit_calls = store.emit_event.call_args_list
        assert any(
            c[0][0] == worker_id and c[0][1] == EventType.USER_MESSAGE
            for c in emit_calls
        )

        # Worker re-enqueued on the shared work queue with its tenant tuple.
        redis.zadd.assert_called_once_with(
            SHARED_WORK_QUEUE_KEY,
            {
                encode_queue_member(
                    org_id=str(org_id),
                    agent_id="agent-test",
                    session_id=str(worker_id),
                ): 0,
            },
        )

    @pytest.mark.asyncio
    async def test_rejects_unowned_worker(self) -> None:
        from surogates.tools.builtin.coordinator import _send_worker_message_handler

        parent_id = uuid4()
        worker_id = uuid4()
        other_parent = uuid4()

        store = _make_store()
        worker = _make_session(id=worker_id, parent_id=other_parent)
        store.get_session = AsyncMock(return_value=worker)

        result = await _send_worker_message_handler(
            {"worker_id": str(worker_id), "message": "Hello"},
            session_store=store,
            redis=_make_redis(),
            session_id=str(parent_id),
        )

        parsed = json.loads(result)
        assert "error" in parsed
        assert "does not belong" in parsed["error"]

    @pytest.mark.asyncio
    async def test_resets_completed_session_status(self) -> None:
        """Continuing a completed worker should reset its status to active."""
        from surogates.tools.builtin.coordinator import _send_worker_message_handler

        parent_id = uuid4()
        worker_id = uuid4()

        store = _make_store()
        worker = _make_session(id=worker_id, parent_id=parent_id)
        worker.status = "completed"
        store.get_session = AsyncMock(return_value=worker)
        store.update_session_status = AsyncMock()

        result = await _send_worker_message_handler(
            {"worker_id": str(worker_id), "message": "Continue with tests"},
            session_store=store,
            redis=_make_redis(),
            session_id=str(parent_id),
        )

        parsed = json.loads(result)
        assert parsed["status"] == "sent"

        # Session status must be reset to active.
        store.update_session_status.assert_called_once_with(worker_id, "active")

    @pytest.mark.asyncio
    async def test_does_not_reset_active_session_status(self) -> None:
        """Sending to an active worker should NOT call update_session_status."""
        from surogates.tools.builtin.coordinator import _send_worker_message_handler

        parent_id = uuid4()
        worker_id = uuid4()

        store = _make_store()
        worker = _make_session(id=worker_id, parent_id=parent_id)
        worker.status = "active"
        store.get_session = AsyncMock(return_value=worker)
        store.update_session_status = AsyncMock()

        await _send_worker_message_handler(
            {"worker_id": str(worker_id), "message": "Keep going"},
            session_store=store,
            redis=_make_redis(),
            session_id=str(parent_id),
        )

        store.update_session_status.assert_not_called()


class TestStopWorker:
    @pytest.mark.asyncio
    async def test_publishes_interrupt(self) -> None:
        from surogates.tools.builtin.coordinator import _stop_worker_handler

        parent_id = uuid4()
        worker_id = uuid4()

        store = _make_store()
        worker = _make_session(id=worker_id, parent_id=parent_id)
        store.get_session = AsyncMock(return_value=worker)

        redis = _make_redis()

        result = await _stop_worker_handler(
            {"worker_id": str(worker_id), "reason": "wrong approach"},
            session_store=store,
            redis=redis,
            session_id=str(parent_id),
        )

        parsed = json.loads(result)
        assert parsed["status"] == "stop_requested"

        redis.publish.assert_called_once_with(
            f"surogates:interrupt:{worker_id}",
            json.dumps({"reason": "wrong approach"}),
        )

    @pytest.mark.asyncio
    async def test_rejects_unowned_worker(self) -> None:
        from surogates.tools.builtin.coordinator import _stop_worker_handler

        parent_id = uuid4()
        worker_id = uuid4()

        store = _make_store()
        worker = _make_session(id=worker_id, parent_id=uuid4())  # different parent
        store.get_session = AsyncMock(return_value=worker)

        result = await _stop_worker_handler(
            {"worker_id": str(worker_id)},
            session_store=store,
            redis=_make_redis(),
            session_id=str(parent_id),
        )

        parsed = json.loads(result)
        assert "error" in parsed


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
        from surogates.tools.builtin.delegate import _delegate_handler

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

        await _delegate_handler(
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
        )
        redis.lpush.assert_not_called()
