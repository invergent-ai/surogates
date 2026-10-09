"""Which events left past the cursor give a wake work to do."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.loop_pending import _actionable_pending_events
from surogates.session.events import EventType
from tests.test_wake_slash_command_gate import _harness, _permissive, _session, _stub_store


def event(id_: int, type_: EventType) -> SimpleNamespace:
    return SimpleNamespace(id=id_, type=type_)


def test_device_wait_events_give_a_wake_no_work():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.DEVICE_RESUMED)]
    assert _actionable_pending_events(events, cursor=4) == []


def test_a_user_message_still_does():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.USER_MESSAGE)]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [6]


# What tells a chat's pane of its browser, and that its user took it over: none is work for the agent.
FOR_THE_PANE = [
    EventType.BROWSER_CONTROL_GRANTED,
    EventType.BROWSER_PROVISIONED,
    EventType.BROWSER_DESTROYED,
    EventType.BROWSER_UNAVAILABLE,
]


@pytest.mark.parametrize("told", FOR_THE_PANE, ids=lambda kind: kind.value)
def test_a_browsers_take_over_and_its_opening_and_closing_give_a_wake_no_work(told):
    assert _actionable_pending_events([event(5, told)], cursor=4) == []


def test_a_browsers_hand_back_still_does():
    # Handed back, the agent goes on: the hand back is what wakes it.
    events = [event(5, EventType.BROWSER_CONTROL_GRANTED), event(6, EventType.BROWSER_CONTROL_RETURNED)]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [6]


async def _wake(monkeypatch, *since_the_turn: EventType) -> tuple[int, list[EventType]]:
    """Wake a local-folder chat whose last turn ended with an answer, *since_the_turn* landing after it:
    how many turns the wake ran, and what it wrote to the chat's log."""
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    session = _session()
    session.config.update({"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/home/u/project"})
    events = [
        SimpleNamespace(id=1, type=EventType.USER_MESSAGE.value, data={"content": "Open the report."}),
        SimpleNamespace(id=2, type=EventType.LLM_RESPONSE.value, data={"message": {"role": "assistant", "content": "It is open."}}),
        *[
            SimpleNamespace(id=3 + n, type=kind.value, data={"session_id": str(session.id), "computer": True})
            for n, kind in enumerate(since_the_turn)
        ],
    ]
    store = _stub_store(session, events)
    store.get_harness_cursor = AsyncMock(return_value=2)
    harness = _harness(store, _permissive())
    # The helper's compressor is a spec mock: left alone it hands the turn a mock instead of the messages.
    harness._compressor.prune_stale_browser_states = lambda messages: messages
    harness._run_loop = AsyncMock()
    await asyncio.wait_for(harness.wake(session.id), 5.0)
    return harness._run_loop.await_count, [call.args[1] for call in store.emit_event.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("told", FOR_THE_PANE, ids=lambda kind: kind.value)
async def test_a_wake_that_finds_only_a_take_over_or_the_browsers_opening_or_closing_runs_no_turn(monkeypatch, told):
    # Its user took the browser over to stop the agent: a wake for it gives the agent no turn, and writes nothing.
    assert await _wake(monkeypatch, told) == (0, [])


@pytest.mark.asyncio
async def test_a_wake_at_a_hand_back_runs_the_agents_turn(monkeypatch):
    turns, wrote = await _wake(monkeypatch, EventType.BROWSER_CONTROL_GRANTED, EventType.BROWSER_CONTROL_RETURNED)
    assert turns == 1
    assert wrote == [EventType.HARNESS_WAKE]
