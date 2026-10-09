"""A wake of a local-folder session resumes its unanswered calls before it compacts or asks the model for more."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.session.events import EventType
from tests.test_wake_slash_command_gate import _harness, _permissive, _session, _stub_store

pytestmark = pytest.mark.asyncio

CALLS = [
    {"id": "a", "type": "function", "function": {"name": "terminal", "arguments": "{}"}},
    {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
]


def _events() -> list:
    # b's result moved the cursor past a, which still waits on the computer.
    return [
        SimpleNamespace(id=9, type=EventType.LLM_RESPONSE.value, data={"message": {"role": "assistant", "tool_calls": CALLS}}),
        SimpleNamespace(id=10, type=EventType.TOOL_CALL.value, data={"tool_call_id": "a"}),
        SimpleNamespace(id=11, type=EventType.TOOL_CALL.value, data={"tool_call_id": "b"}),
        SimpleNamespace(id=12, type=EventType.TOOL_RESULT.value, data={"tool_call_id": "b"}),
    ]


async def _wake(
    monkeypatch, config: dict, *, while_running=None, before_replay=None, events: list | None = None, cursor: int = 12,
) -> tuple[list[str], list[dict]]:
    """Wake a session whose log is *events*, read up to *cursor*; *while_running* and *before_replay* are
    called with the harness to inject behaviour."""
    order: list[str] = []
    compacted: list[dict] = []
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))

    async def fake_execute_single_tool(call, **kwargs):
        order.append(f"run {call['id']} as {kwargs['replay_of']}")
        if while_running is not None:
            await while_running(harness)
        return {"role": "tool", "tool_call_id": call["id"], "content": "resumed"}

    monkeypatch.setattr(loop_module, "execute_single_tool", fake_execute_single_tool)
    session = _session()
    session.config.update(config)
    store = _stub_store(session, _events() if events is None else events)
    store.get_harness_cursor = AsyncMock(return_value=cursor)
    harness = _harness(store, _permissive())
    # The helper's compressor is a spec mock: left alone it hands compaction a mock instead of the messages.
    harness._compressor.prune_stale_browser_states = lambda messages: messages

    def rebuild(*a, **k):
        # The step just before the replay.
        if before_replay is not None:
            before_replay(harness)
        return [{"role": "user", "content": "go"}, {"role": "assistant", "content": "", "tool_calls": CALLS}, {"role": "tool", "tool_call_id": "b", "content": "read"}]

    harness._rebuild_messages = rebuild

    async def engineer(_session, _events, messages):
        order.append("compact")
        compacted.extend(messages)
        return messages

    harness._engineer_context = engineer
    harness._run_loop = AsyncMock(side_effect=lambda *a, **k: order.append("loop"))

    async def journaled(session_factory, calling_session_id, invocations):
        return set(invocations)

    monkeypatch.setattr("surogates.harness.device_replay._journaled", journaled)
    await asyncio.wait_for(harness.wake(session.id), 5.0)
    return order, compacted


async def test_a_local_folder_wake_resumes_a_call_a_sibling_hid_before_it_compacts(monkeypatch):
    config = {"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/home/u/project"}
    order, compacted = await _wake(monkeypatch, config)
    assert order == ["run a as 10", "compact", "loop"]
    assert {"role": "tool", "tool_call_id": "a", "content": "resumed"} in compacted


async def test_a_cloud_wake_runs_the_turn_of_a_call_a_sibling_hid_and_resumes_nothing(monkeypatch):
    # Its worker died with the call begun. No journal says what it did, so nothing is resumed: the turn
    # runs again, and the model is told that call's result is unavailable.
    order, _ = await _wake(monkeypatch, {})
    assert order == ["compact", "loop"]


async def test_a_cloud_wake_with_every_call_answered_and_nothing_pending_does_nothing(monkeypatch):
    answered = [*_events(), SimpleNamespace(id=13, type=EventType.TOOL_RESULT.value, data={"tool_call_id": "a"})]
    order, _ = await _wake(monkeypatch, {}, events=answered, cursor=13)
    assert order == []


@pytest.mark.parametrize("opened", ["before", "after"])
async def test_a_cloud_wake_runs_that_turn_whichever_side_of_the_siblings_result_its_browser_opened(monkeypatch, opened):
    # The call that opened the browser is the one left unanswered; its sibling's result moved the cursor.
    result = SimpleNamespace(type=EventType.TOOL_RESULT.value, data={"tool_call_id": "b"})
    browser = SimpleNamespace(type=EventType.BROWSER_PROVISIONED.value, data={"session_id": "s", "browser_id": "b-1"})
    tail = [browser, result] if opened == "before" else [result, browser]
    for at, event in enumerate(tail, start=12):
        event.id = at
    order, _ = await _wake(monkeypatch, {}, events=[*_events()[:3], *tail], cursor=result.id)
    assert order == ["compact", "loop"]


LOCAL = {"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/home/u/project"}


async def _waits_forever(harness) -> None:
    await asyncio.Event().wait()


async def test_a_pause_while_a_resumed_call_waits_stops_the_wake_before_it_compacts(monkeypatch):
    async def pause_then_wait(harness) -> None:
        harness.interrupt("paused by user")
        await _waits_forever(harness)

    order, _ = await _wake(monkeypatch, LOCAL, while_running=pause_then_wait)
    assert order == ["run a as 10"]


async def test_a_pause_that_landed_before_the_replay_still_stops_it(monkeypatch):
    order, _ = await _wake(
        monkeypatch, LOCAL, while_running=_waits_forever,
        before_replay=lambda harness: harness.interrupt("paused by user"),
    )
    assert "compact" not in order and "loop" not in order
