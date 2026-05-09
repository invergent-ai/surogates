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
