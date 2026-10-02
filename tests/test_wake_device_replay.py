"""A wake of a local-folder session resumes its unanswered calls before it compacts or asks the model for more."""

from __future__ import annotations

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


async def _wake(monkeypatch, config: dict) -> tuple[list[str], list[dict]]:
    order: list[str] = []
    compacted: list[dict] = []
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))

    async def fake_execute_single_tool(call, **kwargs):
        order.append(f"run {call['id']} as {call['_replay_of']}")
        return {"role": "tool", "tool_call_id": call["id"], "content": "resumed"}

    monkeypatch.setattr(loop_module, "execute_single_tool", fake_execute_single_tool)
    session = _session()
    session.config.update(config)
    store = _stub_store(session, _events())
    store.get_harness_cursor = AsyncMock(return_value=12)
    harness = _harness(store, _permissive())
    # The helper's compressor is a spec mock: left alone it hands compaction a mock instead of the messages.
    harness._compressor.prune_stale_browser_states = lambda messages: messages
    harness._rebuild_messages = lambda *a, **k: [{"role": "user", "content": "go"}, {"role": "assistant", "content": "", "tool_calls": CALLS}, {"role": "tool", "tool_call_id": "b", "content": "read"}]

    async def engineer(_session, _events, messages):
        order.append("compact")
        compacted.extend(messages)
        return messages

    harness._engineer_context = engineer
    harness._run_loop = AsyncMock(side_effect=lambda *a, **k: order.append("loop"))

    async def journaled(session_factory, calling_session_id, invocations):
        return set(invocations)

    monkeypatch.setattr("surogates.harness.device_replay._journaled", journaled)
    await harness.wake(session.id)
    return order, compacted


async def test_a_local_folder_wake_resumes_a_call_a_sibling_hid_before_it_compacts(monkeypatch):
    config = {"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/home/u/project"}
    order, compacted = await _wake(monkeypatch, config)
    assert order == ["run a as 10", "compact", "loop"]
    assert {"role": "tool", "tool_call_id": "a", "content": "resumed"} in compacted


async def test_a_cloud_wake_with_nothing_pending_does_nothing(monkeypatch):
    order, _ = await _wake(monkeypatch, {})
    assert order == []
