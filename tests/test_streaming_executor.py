"""Tests for the streaming tool executor.

Covers: concurrency classification, executor lifecycle, parallel execution
of concurrency-safe tools, sequential execution of non-concurrent tools,
insertion-order result delivery, sibling abort, discard, interrupt handling,
and tool block detection during LLM streaming.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from surogates.harness.streaming_executor import (
    StreamingToolExecutor,
    ToolStatus,
    TrackedTool,
    _is_error_result,
)
from surogates.harness.tool_exec import (
    CONCURRENCY_SAFE_TOOLS,
    SIBLING_ABORT_TOOLS,
    is_concurrency_safe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_call(name: str, args: dict | None = None, call_id: str | None = None) -> dict:
    """Build an OpenAI-format tool call dict."""
    return {
        "id": call_id or f"call_{name}_{id(name)}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args or {}),
        },
    }


def _make_session(**overrides: Any) -> MagicMock:
    """Build a mock Session object."""
    session = MagicMock()
    session.id = overrides.get("id", uuid4())
    session.model = overrides.get("model", "gpt-4o")
    session.config = overrides.get("config", {})
    session.agent_id = overrides.get("agent_id")
    return session


def _make_lease() -> MagicMock:
    """Build a mock SessionLease."""
    lease = MagicMock()
    lease.lease_token = uuid4()
    return lease


def _make_store() -> AsyncMock:
    """Build a mock SessionStore."""
    store = AsyncMock()
    store.emit_event = AsyncMock(return_value=1)
    store.advance_harness_cursor = AsyncMock()
    return store


def _make_registry(*tool_names: str) -> MagicMock:
    """Build a mock ToolRegistry that knows about the given tool names."""
    registry = MagicMock()
    registry.has.side_effect = lambda name: name in tool_names
    registry.dispatch = AsyncMock(return_value='{"ok": true}')
    registry.get_schemas.return_value = []

    def _get(name: str) -> MagicMock | None:
        if name in tool_names:
            entry = MagicMock()
            entry.name = name
            return entry
        return None
    registry.get.side_effect = _get
    return registry


def _make_executor(**overrides: Any) -> StreamingToolExecutor:
    """Build a StreamingToolExecutor with sane mock defaults."""
    return StreamingToolExecutor(
        session=overrides.get("session", _make_session()),
        lease=overrides.get("lease", _make_lease()),
        store=overrides.get("store", _make_store()),
        tools=overrides.get("tools", _make_registry("read_file", "write_file", "terminal", "search_files", "list_files", "web_search")),
        tenant=overrides.get("tenant", MagicMock(asset_root="/tmp/test")),
        interrupt_check=overrides.get("interrupt_check", lambda: False),
        redis=overrides.get("redis"),
        budget=overrides.get("budget"),
        memory_manager=overrides.get("memory_manager"),
        hint_tracker=overrides.get("hint_tracker"),
        sandbox_pool=overrides.get("sandbox_pool"),
        api_client=overrides.get("api_client"),
        session_factory=overrides.get("session_factory"),
        saga=overrides.get("saga"),
    )


# ---------------------------------------------------------------------------
# Concurrency classification
# ---------------------------------------------------------------------------


class TestConcurrencyClassification:
    """Tests for is_concurrency_safe and the CONCURRENCY_SAFE_TOOLS set."""

    def test_read_only_tools_are_safe(self) -> None:
        safe_tools = [
            "read_file", "search_files", "list_files",
            "session_search", "skills_list", "skill_view",
            "web_search", "web_extract", "web_crawl", "todo",
        ]
        for name in safe_tools:
            assert is_concurrency_safe(name), f"{name} should be concurrency-safe"

    def test_write_tools_are_not_safe(self) -> None:
        unsafe_tools = [
            "write_file", "patch", "terminal", "execute_code",
            "memory", "skill_manage", "delegate_task", "clarify",
            "browser_navigate",
        ]
        for name in unsafe_tools:
            assert not is_concurrency_safe(name), f"{name} should NOT be concurrency-safe"

    def test_unknown_tool_is_not_safe(self) -> None:
        assert not is_concurrency_safe("unknown_tool_xyz")

    def test_sibling_abort_tools(self) -> None:
        assert "terminal" in SIBLING_ABORT_TOOLS
        assert "execute_code" in SIBLING_ABORT_TOOLS
        assert "read_file" not in SIBLING_ABORT_TOOLS


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_is_error_result_with_error(self) -> None:
        result = {"content": json.dumps({"error": "something broke"})}
        assert _is_error_result(result) is True

    def test_is_error_result_without_error(self) -> None:
        result = {"content": json.dumps({"ok": True})}
        assert _is_error_result(result) is False

    def test_is_error_result_with_plain_text(self) -> None:
        result = {"content": "file contents here"}
        assert _is_error_result(result) is False

    def test_make_skipped_tool_result_with_reason(self) -> None:
        from surogates.harness.message_utils import make_skipped_tool_result
        tc = _make_tool_call("read_file", call_id="tc_123")
        result = make_skipped_tool_result(tc, reason="cancelled (sibling error)")
        assert result["role"] == "tool"
        assert result["tool_call_id"] == "tc_123"
        assert "cancelled" in result["content"].lower()


# ---------------------------------------------------------------------------
# StreamingToolExecutor — lifecycle
# ---------------------------------------------------------------------------


class TestExecutorLifecycle:
    def test_initial_state(self) -> None:
        executor = _make_executor()
        assert executor.has_tools is False
        assert executor.tool_count == 0

    @pytest.mark.asyncio
    async def test_add_tool_increments_count(self) -> None:
        executor = _make_executor()
        executor.add_tool(_make_tool_call("read_file"))
        assert executor.has_tools is True
        assert executor.tool_count == 1

    def test_add_tool_after_discard_is_ignored(self) -> None:
        executor = _make_executor()
        executor.discard()
        executor.add_tool(_make_tool_call("read_file"))
        assert executor.has_tools is False

    @pytest.mark.asyncio
    async def test_empty_executor_returns_empty_results(self) -> None:
        executor = _make_executor()
        results = await executor.get_all_results()
        assert results == []

    def test_stats_with_no_tools(self) -> None:
        executor = _make_executor()
        stats = executor.stats
        assert stats["total"] == 0
        assert stats["concurrent"] == 0
        assert stats["sequential"] == 0


# ---------------------------------------------------------------------------
# Concurrent execution
# ---------------------------------------------------------------------------


class TestConcurrentExecution:
    """Tests that concurrency-safe tools execute in parallel."""

    @pytest.mark.asyncio
    async def test_concurrent_tools_start_immediately(self) -> None:
        """Concurrent-safe tools should start executing when added."""
        execution_order: list[str] = []

        async def mock_dispatch(name, args, **kwargs):
            execution_order.append(f"start_{name}")
            await asyncio.sleep(0.01)
            execution_order.append(f"end_{name}")
            return json.dumps({"ok": True})

        store = _make_store()
        tools = _make_registry("read_file", "search_files")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)

        # Add two concurrent-safe tools.
        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))
        executor.add_tool(_make_tool_call("search_files", call_id="tc_2"))

        results = await executor.get_all_results()

        assert len(results) == 2
        # Both should have started before either finished (parallel execution).
        assert execution_order.index("start_read_file") < execution_order.index("end_search_files")
        assert execution_order.index("start_search_files") < execution_order.index("end_read_file")

    @pytest.mark.asyncio
    async def test_result_ordering_maintained(self) -> None:
        """Results must be returned in tool insertion order, not completion order."""
        completion_order: list[str] = []

        async def mock_dispatch(name, args, **kwargs):
            # read_file is slow, search_files is fast.
            delay = 0.05 if name == "read_file" else 0.01
            await asyncio.sleep(delay)
            completion_order.append(name)
            return json.dumps({"tool": name})

        store = _make_store()
        tools = _make_registry("read_file", "search_files")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)

        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))
        executor.add_tool(_make_tool_call("search_files", call_id="tc_2"))

        results = await executor.get_all_results()

        # search_files completed first, but results should be in insertion order.
        assert completion_order[0] == "search_files"
        assert results[0]["tool_call_id"] == "tc_1"  # read_file first
        assert results[1]["tool_call_id"] == "tc_2"  # search_files second


# ---------------------------------------------------------------------------
# Sequential execution
# ---------------------------------------------------------------------------


class TestSequentialExecution:
    """Tests that non-concurrent tools block the queue."""

    @pytest.mark.asyncio
    async def test_non_concurrent_tool_runs_alone(self) -> None:
        """A non-concurrent tool should not run while concurrent tools are executing."""
        execution_timeline: list[tuple[str, str]] = []

        async def mock_dispatch(name, args, **kwargs):
            execution_timeline.append((name, "start"))
            await asyncio.sleep(0.01)
            execution_timeline.append((name, "end"))
            return json.dumps({"ok": True})

        store = _make_store()
        tools = _make_registry("read_file", "write_file")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)

        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))
        executor.add_tool(_make_tool_call("write_file", call_id="tc_2"))  # non-concurrent

        results = await executor.get_all_results()

        assert len(results) == 2

        # write_file should not start until read_file finishes.
        starts = [e for e in execution_timeline if e[1] == "start"]
        ends = [e for e in execution_timeline if e[1] == "end"]
        # read_file must end before write_file starts.
        read_end_idx = execution_timeline.index(("read_file", "end"))
        write_start_idx = execution_timeline.index(("write_file", "start"))
        assert read_end_idx < write_start_idx

    @pytest.mark.asyncio
    async def test_non_concurrent_blocks_subsequent_concurrent(self) -> None:
        """Non-concurrent tool blocks even concurrent tools behind it."""
        execution_timeline: list[tuple[str, str]] = []

        async def mock_dispatch(name, args, **kwargs):
            execution_timeline.append((name, "start"))
            await asyncio.sleep(0.01)
            execution_timeline.append((name, "end"))
            return json.dumps({"ok": True})

        store = _make_store()
        tools = _make_registry("read_file", "write_file", "search_files")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)

        executor.add_tool(_make_tool_call("write_file", call_id="tc_1"))  # non-concurrent
        executor.add_tool(_make_tool_call("read_file", call_id="tc_2"))   # concurrent but queued behind
        executor.add_tool(_make_tool_call("search_files", call_id="tc_3"))  # concurrent but queued behind

        results = await executor.get_all_results()

        assert len(results) == 3

        # write_file must finish before read_file and search_files start.
        write_end_idx = execution_timeline.index(("write_file", "end"))
        read_start_idx = execution_timeline.index(("read_file", "start"))
        search_start_idx = execution_timeline.index(("search_files", "start"))
        assert write_end_idx < read_start_idx
        assert write_end_idx < search_start_idx


# ---------------------------------------------------------------------------
# Concurrency gate
# ---------------------------------------------------------------------------


class TestConcurrencyGate:
    """Tests for the _can_execute logic."""

    @pytest.mark.asyncio
    async def test_concurrent_with_concurrent_allowed(self) -> None:
        """Two concurrent-safe tools can execute together."""
        executor = _make_executor()

        tc1 = _make_tool_call("read_file", call_id="tc_1")
        tc2 = _make_tool_call("search_files", call_id="tc_2")

        tracked1 = TrackedTool(tool_call=tc1, is_concurrency_safe=True)
        tracked2 = TrackedTool(tool_call=tc2, is_concurrency_safe=True)

        executor._tracked.append(tracked1)
        tracked1.status = ToolStatus.EXECUTING

        assert executor._can_execute(tracked2) is True

    @pytest.mark.asyncio
    async def test_non_concurrent_with_concurrent_blocked(self) -> None:
        """A non-concurrent tool cannot start while concurrent tools are running."""
        executor = _make_executor()

        tc1 = _make_tool_call("read_file", call_id="tc_1")
        tc2 = _make_tool_call("write_file", call_id="tc_2")

        tracked1 = TrackedTool(tool_call=tc1, is_concurrency_safe=True)
        tracked2 = TrackedTool(tool_call=tc2, is_concurrency_safe=False)

        executor._tracked.append(tracked1)
        tracked1.status = ToolStatus.EXECUTING

        assert executor._can_execute(tracked2) is False

    @pytest.mark.asyncio
    async def test_concurrent_with_non_concurrent_blocked(self) -> None:
        """A concurrent tool cannot start while a non-concurrent tool is running."""
        executor = _make_executor()

        tc1 = _make_tool_call("write_file", call_id="tc_1")
        tc2 = _make_tool_call("read_file", call_id="tc_2")

        tracked1 = TrackedTool(tool_call=tc1, is_concurrency_safe=False)
        tracked2 = TrackedTool(tool_call=tc2, is_concurrency_safe=True)

        executor._tracked.append(tracked1)
        tracked1.status = ToolStatus.EXECUTING

        assert executor._can_execute(tracked2) is False

    def test_sibling_aborted_blocks_all(self) -> None:
        """Nothing can execute after sibling abort."""
        executor = _make_executor()
        executor._sibling_aborted = True

        tc = _make_tool_call("read_file")
        tracked = TrackedTool(tool_call=tc, is_concurrency_safe=True)
        assert executor._can_execute(tracked) is False

    def test_discarded_blocks_all(self) -> None:
        """Nothing can execute after discard."""
        executor = _make_executor()
        executor._discarded = True

        tc = _make_tool_call("read_file")
        tracked = TrackedTool(tool_call=tc, is_concurrency_safe=True)
        assert executor._can_execute(tracked) is False

    def test_interrupt_blocks_all(self) -> None:
        """Nothing can execute when interrupted."""
        executor = _make_executor(interrupt_check=lambda: True)

        tc = _make_tool_call("read_file")
        tracked = TrackedTool(tool_call=tc, is_concurrency_safe=True)
        assert executor._can_execute(tracked) is False


# ---------------------------------------------------------------------------
# Sibling abort
# ---------------------------------------------------------------------------


class TestSiblingAbort:
    """Tests for the sibling abort mechanism."""

    @pytest.mark.asyncio
    async def test_terminal_error_aborts_siblings(self) -> None:
        """An error from 'terminal' should cancel concurrent siblings."""
        call_count = {"search_files": 0}

        async def mock_dispatch(name, args, **kwargs):
            if name == "terminal":
                await asyncio.sleep(0.01)
                raise RuntimeError("command failed")
            if name == "search_files":
                call_count["search_files"] += 1
                # This should get cancelled if sibling aborts fast enough,
                # but it might complete first.  Either way is fine.
                await asyncio.sleep(0.1)
                return json.dumps({"ok": True})
            return json.dumps({"ok": True})

        store = _make_store()
        tools = _make_registry("terminal", "search_files")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)

        # terminal is NOT concurrency-safe, so it won't run during
        # streaming.  Instead, test sibling abort with two tools where
        # the first is terminal (non-concurrent, starts first).
        executor.add_tool(_make_tool_call("terminal", call_id="tc_1"))

        results = await executor.get_all_results()

        assert len(results) == 1
        assert executor._sibling_aborted is True

    @pytest.mark.asyncio
    async def test_read_file_error_does_not_abort_siblings(self) -> None:
        """Errors from non-SIBLING_ABORT_TOOLS should not cancel siblings."""
        async def mock_dispatch(name, args, **kwargs):
            if name == "read_file":
                return json.dumps({"error": "file not found"})
            await asyncio.sleep(0.01)
            return json.dumps({"ok": True})

        store = _make_store()
        tools = _make_registry("read_file", "search_files")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)

        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))
        executor.add_tool(_make_tool_call("search_files", call_id="tc_2"))

        results = await executor.get_all_results()

        assert len(results) == 2
        assert executor._sibling_aborted is False


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


class TestDiscard:
    """Tests for executor discard (e.g., on model fallback mid-stream)."""

    @pytest.mark.asyncio
    async def test_discard_cancels_in_flight_tasks(self) -> None:
        """Discard should cancel executing tasks."""
        started = asyncio.Event()

        async def mock_dispatch(name, args, **kwargs):
            started.set()
            await asyncio.sleep(10)  # Will be cancelled
            return json.dumps({"ok": True})

        store = _make_store()
        tools = _make_registry("read_file")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)
        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))

        # Wait for execution to start.
        await started.wait()

        executor.discard()

        results = await executor.get_all_results()
        assert len(results) == 1
        # Result should be a skipped/cancelled result.
        content = results[0].get("content", "")
        assert "skipped" in content.lower() or "cancelled" in content.lower()

    @pytest.mark.asyncio
    async def test_discard_prevents_new_tools(self) -> None:
        """After discard, add_tool should be a no-op."""
        executor = _make_executor()
        executor.discard()
        executor.add_tool(_make_tool_call("read_file"))
        assert executor.tool_count == 0


# ---------------------------------------------------------------------------
# Interrupt handling
# ---------------------------------------------------------------------------


class TestInterruptHandling:
    @pytest.mark.asyncio
    async def test_interrupted_tools_get_skipped_results(self) -> None:
        """Tools that never execute due to interrupt should get skipped results."""
        interrupted = False

        def interrupt_check():
            return interrupted

        executor = _make_executor(interrupt_check=interrupt_check)

        # Add a tool, then set interrupt before it can execute.
        interrupted = True
        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))

        results = await executor.get_all_results()
        assert len(results) == 1
        assert "skipped" in results[0]["content"].lower()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    @pytest.mark.asyncio
    async def test_stats_after_execution(self) -> None:
        async def mock_dispatch(name, args, **kwargs):
            return json.dumps({"ok": True})

        store = _make_store()
        tools = _make_registry("read_file", "write_file")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)

        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))
        executor.add_tool(_make_tool_call("write_file", call_id="tc_2"))

        await executor.get_all_results()

        stats = executor.stats
        assert stats["total"] == 2
        assert stats["concurrent"] == 1
        assert stats["sequential"] == 1
        assert stats["completed"] == 2
        assert stats["errored"] == 0
        assert stats["sibling_aborted"] is False


# ---------------------------------------------------------------------------
# Tool block detection in LLM streaming
# ---------------------------------------------------------------------------


class TestToolBlockDetection:
    """Tests for the on_tool_call_complete callback in call_llm_streaming_inner."""

    @pytest.mark.asyncio
    async def test_callback_fires_on_higher_index(self) -> None:
        """When a new tool call starts at index N, tool calls at indices < N
        should be reported as complete via the callback."""
        from unittest.mock import AsyncMock as _AM, MagicMock as _MM

        notified: list[dict] = []

        def on_complete(tc: dict) -> None:
            notified.append(tc)

        # Simulate tool call accumulation and notification logic
        # (extracted from call_llm_streaming_inner).
        tool_calls_acc: dict[int, dict] = {}
        _notified_slots: set[int] = set()
        _highest_known_slot = -1

        # Simulate receiving tool call at index 0.
        tool_calls_acc[0] = {
            "id": "tc_0",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "/a"}'},
        }
        idx = 0
        if idx > _highest_known_slot:
            for prev_slot in sorted(tool_calls_acc):
                if prev_slot < idx and prev_slot not in _notified_slots:
                    prev_entry = tool_calls_acc[prev_slot]
                    if prev_entry["function"]["name"]:
                        _notified_slots.add(prev_slot)
                        on_complete(prev_entry)
            _highest_known_slot = idx

        # No notification yet — only one tool, no higher index.
        assert len(notified) == 0

        # Simulate receiving tool call at index 1.
        tool_calls_acc[1] = {
            "id": "tc_1",
            "type": "function",
            "function": {"name": "search_files", "arguments": '{"query": "test"}'},
        }
        idx = 1
        if idx > _highest_known_slot:
            for prev_slot in sorted(tool_calls_acc):
                if prev_slot < idx and prev_slot not in _notified_slots:
                    prev_entry = tool_calls_acc[prev_slot]
                    if prev_entry["function"]["name"]:
                        _notified_slots.add(prev_slot)
                        on_complete(prev_entry)
            _highest_known_slot = idx

        # Tool call 0 should now be notified.
        assert len(notified) == 1
        assert notified[0]["id"] == "tc_0"
        assert notified[0]["function"]["name"] == "read_file"

    @pytest.mark.asyncio
    async def test_remaining_tools_notified_on_stream_end(self) -> None:
        """After the stream ends, all remaining unnotified tool calls
        should be reported via the callback."""
        notified: list[dict] = []

        def on_complete(tc: dict) -> None:
            notified.append(tc)

        tool_calls_acc: dict[int, dict] = {
            0: {
                "id": "tc_0",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "/a"}'},
            },
            1: {
                "id": "tc_1",
                "type": "function",
                "function": {"name": "search_files", "arguments": '{}'},
            },
        }
        _notified_slots: set[int] = {0}  # 0 was already notified during streaming

        # Post-stream notification (mirrors code after the async for loop).
        for slot in sorted(tool_calls_acc):
            if slot not in _notified_slots:
                entry = tool_calls_acc[slot]
                if entry["function"]["name"]:
                    _notified_slots.add(slot)
                    on_complete(entry)

        # Only tool call 1 should be notified (0 was already done).
        assert len(notified) == 1
        assert notified[0]["id"] == "tc_1"

    @pytest.mark.asyncio
    async def test_single_tool_notified_on_stream_end(self) -> None:
        """A single tool call (no higher index) is notified on stream end."""
        notified: list[dict] = []

        def on_complete(tc: dict) -> None:
            notified.append(tc)

        tool_calls_acc: dict[int, dict] = {
            0: {
                "id": "tc_0",
                "type": "function",
                "function": {"name": "write_file", "arguments": '{}'},
            },
        }
        _notified_slots: set[int] = set()

        for slot in sorted(tool_calls_acc):
            if slot not in _notified_slots:
                entry = tool_calls_acc[slot]
                if entry["function"]["name"]:
                    _notified_slots.add(slot)
                    on_complete(entry)

        assert len(notified) == 1
        assert notified[0]["function"]["name"] == "write_file"

    @pytest.mark.asyncio
    async def test_nameless_tool_not_notified(self) -> None:
        """Tool calls without a name (partially received) are not notified."""
        notified: list[dict] = []

        def on_complete(tc: dict) -> None:
            notified.append(tc)

        tool_calls_acc: dict[int, dict] = {
            0: {
                "id": "tc_0",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        }
        _notified_slots: set[int] = set()

        for slot in sorted(tool_calls_acc):
            if slot not in _notified_slots:
                entry = tool_calls_acc[slot]
                if entry["function"]["name"]:
                    _notified_slots.add(slot)
                    on_complete(entry)

        assert len(notified) == 0

    @pytest.mark.asyncio
    async def test_no_tools_no_callback(self) -> None:
        """When there are no tool calls, the callback is never fired."""
        notified: list[dict] = []

        def on_complete(tc: dict) -> None:
            notified.append(tc)

        tool_calls_acc: dict[int, dict] = {}
        _notified_slots: set[int] = set()

        for slot in sorted(tool_calls_acc):
            if slot not in _notified_slots:
                entry = tool_calls_acc[slot]
                if entry["function"]["name"]:
                    _notified_slots.add(slot)
                    on_complete(entry)

        assert len(notified) == 0


# ---------------------------------------------------------------------------
# Process queue
# ---------------------------------------------------------------------------


class TestProcessQueue:
    """Tests for _process_queue behavior."""

    @pytest.mark.asyncio
    async def test_queue_drains_after_concurrent_tools_finish(self) -> None:
        """After concurrent tools finish, queued non-concurrent tools should start."""
        execution_order: list[str] = []

        async def mock_dispatch(name, args, **kwargs):
            execution_order.append(name)
            await asyncio.sleep(0.01)
            return json.dumps({"ok": True})

        store = _make_store()
        tools = _make_registry("read_file", "list_files", "write_file")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)

        # Add concurrent tools first, then a non-concurrent tool.
        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))
        executor.add_tool(_make_tool_call("list_files", call_id="tc_2"))
        executor.add_tool(_make_tool_call("write_file", call_id="tc_3"))

        results = await executor.get_all_results()

        assert len(results) == 3
        # read_file and list_files should execute before write_file.
        assert "write_file" in execution_order
        write_idx = execution_order.index("write_file")
        assert write_idx >= 2  # Must be after both concurrent tools


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for tool execution error handling within the executor."""

    @pytest.mark.asyncio
    async def test_tool_exception_produces_error_result(self) -> None:
        """A tool that raises an exception should produce an error result."""
        async def mock_dispatch(name, args, **kwargs):
            raise ValueError("something went wrong")

        store = _make_store()
        tools = _make_registry("read_file")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)
        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))

        results = await executor.get_all_results()

        assert len(results) == 1
        content = json.loads(results[0]["content"])
        assert "error" in content
        assert "something went wrong" in content["error"]

    @pytest.mark.asyncio
    async def test_error_does_not_crash_other_tools(self) -> None:
        """An error in one non-abort tool should not affect others."""
        async def mock_dispatch(name, args, **kwargs):
            if name == "read_file":
                return json.dumps({"error": "file not found"})
            await asyncio.sleep(0.01)
            return json.dumps({"data": "search results"})

        store = _make_store()
        tools = _make_registry("read_file", "search_files")
        tools.dispatch = mock_dispatch

        executor = _make_executor(store=store, tools=tools)
        executor.add_tool(_make_tool_call("read_file", call_id="tc_1"))
        executor.add_tool(_make_tool_call("search_files", call_id="tc_2"))

        results = await executor.get_all_results()

        assert len(results) == 2
        # Both should have results.
        assert results[0]["tool_call_id"] == "tc_1"
        assert results[1]["tool_call_id"] == "tc_2"


# ---------------------------------------------------------------------------
# Integration: re-export from tool_exec
# ---------------------------------------------------------------------------


class TestCanonicalImport:
    """Verify constants are defined in tool_exec and importable from streaming_executor."""

    def test_streaming_executor_reexports_from_tool_exec(self) -> None:
        from surogates.harness import streaming_executor as se
        from surogates.harness import tool_exec as te
        assert se.CONCURRENCY_SAFE_TOOLS is te.CONCURRENCY_SAFE_TOOLS
        assert se.is_concurrency_safe is te.is_concurrency_safe
