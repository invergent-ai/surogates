"""Tool-call execution blocks repeated failures and no-progress loops."""

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
)
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema


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


_TERMINAL_OK = '{"exit_code":0,"stdout":"1499 shadow_strike_v3.html"}'


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
