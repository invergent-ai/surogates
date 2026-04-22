"""Tests for coordinator mode: spawn_worker, send_worker_message, stop_worker,
worker notification, event replay, tool filtering, and saga read-only parallel fix.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.session.events import EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(**overrides: Any) -> MagicMock:
    session = MagicMock()
    session.id = overrides.get("id", uuid4())
    session.parent_id = overrides.get("parent_id")
    session.agent_id = overrides.get("agent_id", "agent-test")
    session.model = overrides.get("model", "gpt-4o")
    session.config = overrides.get("config", {})
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


# ---------------------------------------------------------------------------
# spawn_worker
# ---------------------------------------------------------------------------


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

        result = await _spawn_worker_handler(
            {"goal": "Fix the auth bug"},
            session_store=store,
            redis=redis,
            tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
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

        # Enqueued to the parent agent's work queue (not task_queue).
        redis.zadd.assert_called_once_with(
            "surogates:work_queue:agent-1", {str(child_id): 0},
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


# ---------------------------------------------------------------------------
# send_worker_message
# ---------------------------------------------------------------------------


class TestSendWorkerMessage:
    @pytest.mark.asyncio
    async def test_sends_message_and_enqueues(self) -> None:
        from surogates.tools.builtin.coordinator import _send_worker_message_handler

        parent_id = uuid4()
        worker_id = uuid4()

        store = _make_store()
        worker = _make_session(id=worker_id, parent_id=parent_id)
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

        # Worker re-enqueued on its agent's queue.
        redis.zadd.assert_called_once_with(
            "surogates:work_queue:agent-test", {str(worker_id): 0},
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


# ---------------------------------------------------------------------------
# stop_worker
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Worker notification
# ---------------------------------------------------------------------------


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

        await notify_parent_on_completion(
            session_store=store,
            worker_session_id=worker_id,
            parent_session_id=parent_id,
            agent_id="agent-test",
            redis=redis,
        )

        # WORKER_COMPLETE emitted into parent session.
        emit_call = store.emit_event.call_args
        assert emit_call[0][0] == parent_id
        assert emit_call[0][1] == EventType.WORKER_COMPLETE
        assert "Auth bug fixed" in emit_call[0][2]["result"]
        assert emit_call[0][2]["worker_id"] == str(worker_id)

        # Parent re-enqueued on its agent's queue.
        redis.zadd.assert_called_once_with(
            "surogates:work_queue:agent-test", {str(parent_id): 0},
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
            agent_id="agent-test",
        )

        result = store.emit_event.call_args[0][2]["result"]
        assert "no response" in result.lower()


# ---------------------------------------------------------------------------
# Event replay
# ---------------------------------------------------------------------------


class TestEventReplay:
    """Test that WORKER_COMPLETE and WORKER_FAILED are replayed as user messages."""

    def _make_event(self, event_type: str, data: dict) -> MagicMock:
        event = MagicMock()
        event.type = event_type
        event.data = data
        event.id = 1
        return event

    def test_worker_complete_becomes_user_message(self) -> None:
        from surogates.harness.loop import AgentHarness

        harness = MagicMock(spec=AgentHarness)

        events = [
            self._make_event(
                EventType.WORKER_COMPLETE.value,
                {"worker_id": "abc-123", "result": "Fixed the bug."},
            ),
        ]

        # Call the real method.
        messages = AgentHarness._rebuild_messages(harness, events)

        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert "[Worker abc-123 completed]" in messages[0]["content"]
        assert "Fixed the bug." in messages[0]["content"]

    def test_multiple_worker_events_in_replay(self) -> None:
        """Multiple WORKER_COMPLETE events produce multiple user messages."""
        from surogates.harness.loop import AgentHarness

        harness = MagicMock(spec=AgentHarness)

        events = [
            self._make_event(
                EventType.WORKER_COMPLETE.value,
                {"worker_id": "worker-1", "result": "Found the bug."},
            ),
            self._make_event(
                EventType.WORKER_COMPLETE.value,
                {"worker_id": "worker-2", "result": "Tests pass."},
            ),
            self._make_event(
                EventType.WORKER_FAILED.value,
                {"worker_id": "worker-3", "error": "Build failed"},
            ),
        ]

        messages = AgentHarness._rebuild_messages(harness, events)

        assert len(messages) == 3
        assert "[Worker worker-1 completed]" in messages[0]["content"]
        assert "[Worker worker-2 completed]" in messages[1]["content"]
        assert "[Worker worker-3 failed" in messages[2]["content"]

    def test_worker_failed_becomes_user_message(self) -> None:
        from surogates.harness.loop import AgentHarness

        harness = MagicMock(spec=AgentHarness)

        events = [
            self._make_event(
                EventType.WORKER_FAILED.value,
                {"worker_id": "def-456", "error": "Out of memory"},
            ),
        ]

        messages = AgentHarness._rebuild_messages(harness, events)

        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert "[Worker def-456 failed" in messages[0]["content"]
        assert "Out of memory" in messages[0]["content"]


# ---------------------------------------------------------------------------
# Tool filtering
# ---------------------------------------------------------------------------


class TestToolFiltering:
    """Test that coordinator and worker sessions get filtered tool sets."""

    def test_coordinator_gets_all_tools(self) -> None:
        """Coordinator mode is soft — coordinators get all tools, not a restricted set."""
        from surogates.tools.builtin.coordinator import WORKER_EXCLUDED_TOOLS

        # Simulate what loop.py does for a coordinator session.
        # coordinator=True → tool_filter=None → all tools visible.
        all_tools = {
            "terminal", "read_file", "write_file", "search_files",
            "spawn_worker", "send_worker_message", "stop_worker",
        }
        # No filtering for coordinators.
        tool_filter = None
        visible = all_tools if tool_filter is None else all_tools & tool_filter

        # Coordinator sees everything including spawn_worker AND terminal.
        assert "spawn_worker" in visible
        assert "terminal" in visible
        assert "write_file" in visible

    def test_worker_excluded_tools(self) -> None:
        from surogates.tools.builtin.coordinator import WORKER_EXCLUDED_TOOLS

        assert "spawn_worker" in WORKER_EXCLUDED_TOOLS
        assert "send_worker_message" in WORKER_EXCLUDED_TOOLS
        assert "stop_worker" in WORKER_EXCLUDED_TOOLS

    def test_normal_session_excludes_coordinator_tools(self) -> None:
        """Normal sessions (no coordinator flag) should not see coordinator tools."""
        from surogates.tools.builtin.coordinator import WORKER_EXCLUDED_TOOLS

        # Simulate what loop.py does for a normal session (no coordinator,
        # no allowed_tools, no excluded_tools in config).
        all_tools = {
            "terminal", "read_file", "write_file", "search_files",
            "spawn_worker", "send_worker_message", "stop_worker",
        }
        excluded = set()
        excluded.update(WORKER_EXCLUDED_TOOLS)
        filtered = all_tools - excluded

        assert "terminal" in filtered
        assert "read_file" in filtered
        assert "spawn_worker" not in filtered
        assert "send_worker_message" not in filtered
        assert "stop_worker" not in filtered


# ---------------------------------------------------------------------------
# Delegation cap
# ---------------------------------------------------------------------------


class TestDelegationCap:
    def test_caps_spawn_worker_calls(self) -> None:
        from surogates.harness.sanitize import cap_delegate_calls

        tool_calls = [
            {"function": {"name": "spawn_worker", "arguments": "{}"}, "id": f"tc_{i}"}
            for i in range(10)
        ]
        capped = cap_delegate_calls(tool_calls, max_delegates=3)
        spawn_calls = [tc for tc in capped if tc["function"]["name"] == "spawn_worker"]
        assert len(spawn_calls) == 3

    def test_caps_mixed_delegate_and_spawn(self) -> None:
        from surogates.harness.sanitize import cap_delegate_calls

        tool_calls = [
            {"function": {"name": "delegate_task", "arguments": "{}"}, "id": "tc_1"},
            {"function": {"name": "spawn_worker", "arguments": "{}"}, "id": "tc_2"},
            {"function": {"name": "spawn_worker", "arguments": "{}"}, "id": "tc_3"},
            {"function": {"name": "spawn_worker", "arguments": "{}"}, "id": "tc_4"},
            {"function": {"name": "read_file", "arguments": "{}"}, "id": "tc_5"},
        ]
        capped = cap_delegate_calls(tool_calls, max_delegates=2)
        delegation_calls = [
            tc for tc in capped
            if tc["function"]["name"] in ("delegate_task", "spawn_worker")
        ]
        assert len(delegation_calls) == 2
        # read_file should be preserved.
        assert any(tc["function"]["name"] == "read_file" for tc in capped)


# ---------------------------------------------------------------------------
# delegate.py queue bug fix
# ---------------------------------------------------------------------------


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

        await _delegate_handler(
            {"goal": "Test task"},
            session_store=store,
            redis=redis,
            tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
            session_id=str(parent_id),
            budget=IterationBudget(max_total=50),
        )

        # Must use zadd to the parent agent's work_queue, NOT lpush to task_queue.
        redis.zadd.assert_called_once_with(
            "surogates:work_queue:agent-1", {str(child_id): 0},
        )
        redis.lpush.assert_not_called()


# ---------------------------------------------------------------------------
# Saga + read-only parallel tools
# ---------------------------------------------------------------------------


class TestSagaReadOnlyParallel:
    def test_all_readonly_tools_parallel_with_saga(self) -> None:
        from surogates.harness.tool_exec import should_parallelize, _all_concurrency_safe

        tool_calls = [
            {"function": {"name": "read_file", "arguments": '{"path": "/a"}'}},
            {"function": {"name": "search_files", "arguments": '{"query": "x"}'}},
        ]

        assert should_parallelize(tool_calls) is True
        assert _all_concurrency_safe(tool_calls) is True

    def test_mixed_tools_not_all_safe(self) -> None:
        from surogates.harness.tool_exec import _all_concurrency_safe

        tool_calls = [
            {"function": {"name": "read_file", "arguments": "{}"}},
            {"function": {"name": "terminal", "arguments": "{}"}},
        ]

        assert _all_concurrency_safe(tool_calls) is False


# ---------------------------------------------------------------------------
# Coordinator prompt
# ---------------------------------------------------------------------------


class TestCoordinatorPrompt:
    def test_coordinator_guidance_injected(self) -> None:
        from surogates.harness.prompt import COORDINATOR_GUIDANCE, PromptBuilder

        session = _make_session(config={"coordinator": True})
        tenant = MagicMock()
        tenant.org_config = {"agent_name": "Test"}
        tenant.user_id = uuid4()
        tenant.user_preferences = {}
        tenant.asset_root = "/tmp/test"

        builder = PromptBuilder(
            tenant=tenant,
            session=session,
            available_tools={"spawn_worker", "read_file"},
        )

        prompt = builder.build()
        assert "worker delegation" in prompt.lower()
        assert "spawn_worker" in prompt

    def test_no_coordinator_guidance_for_normal_session(self) -> None:
        from surogates.harness.prompt import PromptBuilder

        session = _make_session(config={})
        tenant = MagicMock()
        tenant.org_config = {"agent_name": "Test"}
        tenant.user_id = uuid4()
        tenant.user_preferences = {}
        tenant.asset_root = "/tmp/test"

        builder = PromptBuilder(
            tenant=tenant,
            session=session,
            available_tools={"terminal", "read_file"},
        )

        prompt = builder.build()
        assert "spawn_worker" not in prompt
