"""Tool-call execution blocks repeated failures and no-progress loops.

The guardrails are the turn's: each response's calls run through a tool
executor of their own, and a repeat is the next response's call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.streaming_executor import StreamingToolExecutor
from surogates.harness.tool_guardrails import (
    ToolGuardrailConfig,
    ToolGuardrails,
)
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema


async def _responses(calls: int, tool_call: dict, guardrails: ToolGuardrails, **turn: object) -> list[dict]:
    """*tool_call* as the only call of *calls* responses in a row, each run as the turn's executor runs it;
    the turn ends, as the loop ends it, once the guardrails halt it."""
    results = []
    for _ in range(calls):
        if guardrails.halt_decision is not None:
            break
        executor = StreamingToolExecutor(
            interrupt_check=lambda: False, tenant=SimpleNamespace(asset_root="/tmp/test"),
            tool_guardrails=guardrails, **turn,
        )
        executor.add_tool(dict(tool_call))
        results.extend(await executor.get_all_results())
    return results


@pytest.mark.asyncio
async def test_a_turn_blocks_a_repeated_exact_failure_with_events() -> None:
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

    results = await _responses(3, tool_call, guardrails, session=session, lease=lease, store=store, tools=registry)

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
async def test_a_turn_blocks_consecutive_no_progress_by_default() -> None:
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

    results = await _responses(5, tool_call, guardrails, session=session, lease=lease, store=store, tools=registry)

    # 3 executions, 4th blocked, 5th never asked (the halt ends the turn).
    assert handler.await_count == 3
    assert len(results) == 4
    blocked = json.loads(results[3]["content"])
    assert blocked["guardrail"]["code"] == "consecutive_no_progress_block"
    assert guardrails.halt_decision is not None


def test_a_result_carrying_error_null_is_not_a_failure() -> None:
    # The memory tool answers every write with ``"error": null``; matching the
    # key name counted each successful write as a failure, warned "failed 3
    # times" on a PROD memory import, and would halt the session at eight.
    from surogates.harness.tool_guardrails import classify_tool_failure

    ok = json.dumps({"success": True, "message": "Entry added.", "error": None})
    assert classify_tool_failure("memory", ok) is False
    assert classify_tool_failure("memory", json.dumps({"success": False, "error": "over limit"})) is True
    assert classify_tool_failure("web_extract", "Error: connection refused") is True
