"""_collect_steer_messages pulls + coalesces post-cursor user messages."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from surogates.harness.loop import AgentHarness


def _event(eid: int, data: dict):
    return SimpleNamespace(id=eid, data=data)


def _harness(events):
    harness = AgentHarness.__new__(AgentHarness)
    store = AsyncMock()
    store.get_events = AsyncMock(return_value=events)
    harness._store = store
    return harness, store


async def test_no_new_events_returns_none_and_same_cursor():
    harness, _ = _harness([])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg is None
    assert cursor == 10


async def test_single_user_message_is_rendered_and_cursor_advances():
    harness, store = _harness([_event(11, {"content": "steer me"})])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg == {"role": "user", "content": "steer me"}
    assert cursor == 11
    # queried only USER_MESSAGE events past the cursor
    _, kwargs = store.get_events.await_args
    assert kwargs["after"] == 10


async def test_multiple_messages_coalesced_into_one_turn():
    harness, _ = _harness([
        _event(11, {"content": "first"}),
        _event(12, {"content": "second"}),
    ])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg == {"role": "user", "content": "first\n\nsecond"}
    assert cursor == 12


async def test_synthetic_messages_skipped_but_cursor_advances():
    harness, _ = _harness([
        _event(11, {"content": "nudge", "synthetic": "x"}),
        _event(12, {"content": "real"}),
    ])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg == {"role": "user", "content": "real"}
    assert cursor == 12


async def test_all_synthetic_returns_none_but_advances_cursor():
    harness, _ = _harness([_event(11, {"content": "nudge", "synthetic": "x"})])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg is None
    assert cursor == 11
