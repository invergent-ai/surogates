"""The loop joins its turn while it waits for tool calls, so a wait inside a tool gives the turn's slots back.

In production the dispatcher's activity counts the loop itself.  Without the
loop stepping out for the tool calls, the turn never looks idle and a tool
that waits keeps both slots.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.runtime.turn_slots import current_turn, turn_waiting
from surogates.tools.registry import ToolRegistry, ToolSchema
from tests.test_steer_loop import _final_response, _make_loop_harness, _make_session
from tests.test_turn_slots import held_turn

pytestmark = pytest.mark.asyncio

# A concurrency-safe tool: the streaming executor starts it while the response is still streaming.
TOOL = "read_file"


def _tool_call_response() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": TOOL, "arguments": "{}"}}]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )


async def _slot_state_during_a_tool_wait(monkeypatch, *, streaming: bool, partial: bool = False) -> list[bool]:
    """Run one turn whose only tool call waits; return whether the worker slot was held during the wait."""
    slots, semaphore, gate = await held_turn()
    held_during_wait: list[bool] = []

    async def handler(arguments, **kwargs):
        async with turn_waiting():
            held_during_wait.append(semaphore.locked())
        return '{"ok": true}'

    registry = ToolRegistry()
    registry.register(
        TOOL,
        ToolSchema(name=TOOL, description="read", parameters={"type": "object", "properties": {}}),
        handler=handler,
    )
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)
    harness._tools = registry
    harness._tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4(), asset_root="/tmp/test")
    harness._streaming_enabled = streaming

    responses = iter([_tool_call_response(), _final_response("done")])

    async def fake_call_llm_with_retry(**kwargs):
        message, usage = next(responses)
        if message["tool_calls"] and kwargs["on_tool_call_complete"] is not None:
            for tool_call in message["tool_calls"]:
                kwargs["on_tool_call_complete"](tool_call)
        if partial and message["tool_calls"]:
            usage = {**usage, "partial_tool_call": True}
        return message, usage

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry)

    token = current_turn.set(slots)
    try:
        async with slots.activity():  # the dispatcher counts the turn's own task
            await harness._run_loop(
                _make_session(), [{"role": "user", "content": "do the task"}],
                "system", SimpleNamespace(lease_token=uuid4()), all_events=[],
            )
            # The loop took the slots back before it went on.
            assert semaphore.locked() and gate.held == 1
    finally:
        current_turn.reset(token)
    return held_during_wait


async def test_a_wait_inside_a_tool_gives_the_slots_back_when_the_loop_runs_it_after_the_response(monkeypatch):
    assert await _slot_state_during_a_tool_wait(monkeypatch, streaming=False) == [False]


async def test_a_wait_inside_a_streamed_tool_gives_the_slots_back(monkeypatch):
    assert await _slot_state_during_a_tool_wait(monkeypatch, streaming=True) == [False]


async def test_a_wait_inside_a_tool_settled_after_a_cut_off_response_gives_the_slots_back(monkeypatch):
    assert await _slot_state_during_a_tool_wait(monkeypatch, streaming=True, partial=True) == [False]
