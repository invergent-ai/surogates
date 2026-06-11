"""Tests for per-turn tool loop guardrails."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.tool_exec import execute_tool_calls
from surogates.harness.tool_guardrails import (
    ToolGuardrailConfig,
    ToolGuardrails,
    canonical_tool_args,
)
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema


def test_canonical_tool_args_are_stable() -> None:
    assert canonical_tool_args({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_exact_failure_warns_after_threshold() -> None:
    guardrails = ToolGuardrails(
        ToolGuardrailConfig(exact_failure_warn_after=2)
    )
    args = {"path": "missing.txt"}

    assert guardrails.before_call("read_file", args).allows_execution is True
    assert guardrails.after_call("read_file", args, '{"error":"missing"}').action == "allow"
    decision = guardrails.after_call("read_file", args, '{"error":"missing"}')

    assert decision.action == "warn"
    assert decision.code == "repeated_exact_failure_warning"
    assert decision.count == 2


def test_exact_failure_blocks_before_repeating_when_hard_stop_enabled() -> None:
    guardrails = ToolGuardrails(
        ToolGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
        )
    )
    args = {"path": "missing.txt"}

    guardrails.after_call("read_file", args, '{"error":"missing"}')
    guardrails.after_call("read_file", args, '{"error":"missing"}')
    decision = guardrails.before_call("read_file", args)

    assert decision.action == "block"
    assert decision.should_halt is True
    assert decision.code == "repeated_exact_failure_block"


def test_same_tool_failures_with_different_args_halt() -> None:
    guardrails = ToolGuardrails(
        ToolGuardrailConfig(
            hard_stop_enabled=True,
            same_tool_failure_warn_after=2,
            same_tool_failure_halt_after=2,
            exact_failure_block_after=99,
        )
    )

    guardrails.after_call("terminal", {"cmd": "false"}, '{"exit_code":1}')
    decision = guardrails.after_call("terminal", {"cmd": "nope"}, '{"exit_code":2}')

    assert decision.action == "halt"
    assert decision.code == "same_tool_failure_halt"
    assert decision.count == 2


def test_idempotent_no_progress_warns_and_blocks() -> None:
    guardrails = ToolGuardrails(
        ToolGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=2,
        )
    )
    args = {"path": "unchanged.txt"}
    result = '{"content":"same"}'

    assert guardrails.after_call("read_file", args, result).action == "allow"
    warning = guardrails.after_call("read_file", args, result)
    blocked = guardrails.before_call("read_file", args)

    assert warning.action == "warn"
    assert warning.code == "idempotent_no_progress_warning"
    assert blocked.action == "block"
    assert blocked.code == "idempotent_no_progress_block"


@pytest.mark.asyncio
async def test_execute_tool_calls_blocks_repeated_exact_failure_with_events() -> None:
    registry = ToolRegistry()
    handler = AsyncMock(return_value='{"error":"still missing"}')
    registry.register(
        "read_file",
        ToolSchema(
            name="read_file",
            description="read file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
        handler=handler,
    )
    session = SimpleNamespace(
        id=uuid4(),
        config={"workspace_path": ""},
        agent_id="agent",
    )
    lease = SimpleNamespace(lease_token=uuid4())
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=[1, 2, 3, 4, 5, 6])
    store.advance_harness_cursor = AsyncMock()
    guardrails = ToolGuardrails(
        ToolGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
        )
    )
    tool_call = {
        "id": "call_1",
        "function": {"name": "read_file", "arguments": '{"path":"missing.txt"}'},
    }

    results = await execute_tool_calls(
        [tool_call, tool_call, tool_call],
        session=session,
        lease=lease,
        store=store,
        tools=registry,
        tenant=SimpleNamespace(asset_root="/tmp/test"),
        interrupt_check=lambda: False,
        tool_guardrails=guardrails,
    )

    assert handler.await_count == 2
    assert len(results) == 3
    assert "Tool loop warning" in results[1]["content"]

    blocked = json.loads(results[2]["content"])
    assert blocked["guardrail"]["action"] == "block"
    assert blocked["guardrail"]["code"] == "repeated_exact_failure_block"
    assert blocked["tool"] == "read_file"

    event_types = [call.args[1] for call in store.emit_event.await_args_list]
    assert event_types == [
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
    ]


# ---------------------------------------------------------------------------
# Consecutive no-progress guard: identical call + identical result repeated
# back-to-back. Unlike the failure/idempotent paths above, this guard covers
# successful mutating tools too and is enabled by default (independent of
# ``hard_stop_enabled``) — providers such as Qwen reject conversations whose
# history accumulates identical consecutive tool calls, so the harness must
# break the pattern before the provider does.
# ---------------------------------------------------------------------------

_TERMINAL_OK = '{"exit_code":0,"stdout":"1499 shadow_strike_v3.html"}'


def test_consecutive_no_progress_blocks_by_default() -> None:
    """Same successful mutating call 3x with identical results → 4th blocked."""
    guardrails = ToolGuardrails()  # defaults — hard_stop_enabled stays False
    args = {"command": "cat part1 part2 part3 > whole && wc -l whole"}

    for _ in range(3):
        assert guardrails.before_call("terminal", args).allows_execution is True
        guardrails.after_call("terminal", args, _TERMINAL_OK)

    decision = guardrails.before_call("terminal", args)

    assert decision.action == "block"
    assert decision.code == "consecutive_no_progress_block"
    assert decision.should_halt is True
    assert guardrails.halt_decision is decision


def test_consecutive_no_progress_warns_before_block() -> None:
    guardrails = ToolGuardrails()
    args = {"command": "ls"}

    guardrails.before_call("terminal", args)
    assert guardrails.after_call("terminal", args, _TERMINAL_OK).action == "allow"
    guardrails.before_call("terminal", args)
    decision = guardrails.after_call("terminal", args, _TERMINAL_OK)

    assert decision.action == "warn"
    assert decision.code == "consecutive_no_progress_warning"
    assert decision.count == 2


def test_consecutive_no_progress_resets_on_different_call() -> None:
    guardrails = ToolGuardrails()
    run = {"command": "pytest"}
    write = {"path": "x.py", "content": "fix"}

    for _ in range(2):
        guardrails.before_call("terminal", run)
        guardrails.after_call("terminal", run, _TERMINAL_OK)
    guardrails.before_call("write_file", write)
    guardrails.after_call("write_file", write, '{"ok":true}')
    for _ in range(2):
        decision = guardrails.before_call("terminal", run)
        assert decision.allows_execution is True
        guardrails.after_call("terminal", run, _TERMINAL_OK)

    assert guardrails.halt_decision is None


def test_consecutive_identical_calls_with_changing_results_not_blocked() -> None:
    """Polling: identical call whose result keeps changing is progress."""
    guardrails = ToolGuardrails()
    args = {"command": "docker logs app | tail -1"}

    for i in range(6):
        assert guardrails.before_call("terminal", args).allows_execution is True
        guardrails.after_call(
            "terminal", args, json.dumps({"exit_code": 0, "stdout": f"line{i}"})
        )

    assert guardrails.halt_decision is None


def test_consecutive_no_progress_can_be_disabled() -> None:
    guardrails = ToolGuardrails(
        ToolGuardrailConfig.from_mapping({"consecutive_no_progress_enabled": False})
    )
    args = {"command": "ls"}

    for _ in range(6):
        assert guardrails.before_call("terminal", args).allows_execution is True
        guardrails.after_call("terminal", args, _TERMINAL_OK)

    assert guardrails.halt_decision is None


def test_from_mapping_reads_consecutive_no_progress_thresholds() -> None:
    config = ToolGuardrailConfig.from_mapping({
        "warn_after": {"consecutive_no_progress": 4},
        "hard_stop_after": {"consecutive_no_progress": 7},
    })

    assert config.consecutive_no_progress_warn_after == 4
    assert config.consecutive_no_progress_block_after == 7


def _assistant_call_msg(call_id: str, arguments: str) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "terminal", "arguments": arguments},
        }],
    }


def _tool_result_msg(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_seed_from_messages_blocks_repeat_across_wakes() -> None:
    """Trailing identical rounds in rebuilt history count toward the block."""
    guardrails = ToolGuardrails()
    args = {"command": "cat part1 part2 part3 > whole && wc -l whole"}
    raw = canonical_tool_args(args)

    messages = [
        {"role": "user", "content": "combine the files"},
        _assistant_call_msg("c1", raw), _tool_result_msg("c1", _TERMINAL_OK),
        _assistant_call_msg("c2", raw), _tool_result_msg("c2", _TERMINAL_OK),
        _assistant_call_msg("c3", raw), _tool_result_msg("c3", _TERMINAL_OK),
    ]
    guardrails.seed_from_messages(messages)

    decision = guardrails.before_call("terminal", args)

    assert decision.action == "block"
    assert decision.code == "consecutive_no_progress_block"
    assert decision.should_halt is True


def test_seed_from_messages_counts_through_guardrail_synthetic_results() -> None:
    """A previously blocked attempt must not reset the chain on resume."""
    guardrails = ToolGuardrails()
    args = {"command": "cat part1 part2 part3 > whole && wc -l whole"}
    raw = canonical_tool_args(args)
    synthetic = json.dumps({
        "error": "Blocked terminal: no progress",
        "tool": "terminal",
        "guardrail": {"action": "block", "code": "consecutive_no_progress_block"},
    })

    messages = [
        {"role": "user", "content": "combine the files"},
        _assistant_call_msg("c1", raw), _tool_result_msg("c1", _TERMINAL_OK),
        _assistant_call_msg("c2", raw), _tool_result_msg("c2", _TERMINAL_OK),
        _assistant_call_msg("c3", raw), _tool_result_msg("c3", _TERMINAL_OK),
        _assistant_call_msg("c4", raw), _tool_result_msg("c4", synthetic),
    ]
    guardrails.seed_from_messages(messages)

    decision = guardrails.before_call("terminal", args)

    assert decision.action == "block"
    assert decision.should_halt is True


def test_seed_from_messages_resets_on_progress() -> None:
    """A trailing result that differs from the previous one restarts the chain."""
    guardrails = ToolGuardrails(
        ToolGuardrailConfig(consecutive_no_progress_block_after=2)
    )
    args = {"command": "docker logs app | tail -1"}
    raw = canonical_tool_args(args)

    messages = [
        {"role": "user", "content": "watch the logs"},
        _assistant_call_msg("c1", raw),
        _tool_result_msg("c1", '{"exit_code":0,"stdout":"line1"}'),
        _assistant_call_msg("c2", raw),
        _tool_result_msg("c2", '{"exit_code":0,"stdout":"line2"}'),
    ]
    guardrails.seed_from_messages(messages)

    assert guardrails.before_call("terminal", args).allows_execution is True


@pytest.mark.asyncio
async def test_execute_tool_calls_blocks_consecutive_no_progress_by_default() -> None:
    """End-to-end: default guardrails stop a successful identical-call loop."""
    registry = ToolRegistry()
    handler = AsyncMock(return_value=_TERMINAL_OK)
    registry.register(
        "terminal",
        ToolSchema(
            name="terminal",
            description="run a command",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        ),
        handler=handler,
    )
    session = SimpleNamespace(
        id=uuid4(),
        config={"workspace_path": ""},
        agent_id="agent",
    )
    lease = SimpleNamespace(lease_token=uuid4())
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=list(range(1, 20)))
    store.advance_harness_cursor = AsyncMock()
    guardrails = ToolGuardrails()  # defaults
    tool_call = {
        "id": "call_1",
        "function": {
            "name": "terminal",
            "arguments": '{"command":"cat part1 part2 > whole"}',
        },
    }

    results = await execute_tool_calls(
        [tool_call, tool_call, tool_call, tool_call, tool_call],
        session=session,
        lease=lease,
        store=store,
        tools=registry,
        tenant=SimpleNamespace(asset_root="/tmp/test"),
        interrupt_check=lambda: False,
        tool_guardrails=guardrails,
    )

    # 3 executions, 4th blocked, 5th never reached (halt breaks the batch).
    assert handler.await_count == 3
    assert len(results) == 4
    blocked = json.loads(results[3]["content"])
    assert blocked["guardrail"]["code"] == "consecutive_no_progress_block"
    assert guardrails.halt_decision is not None
