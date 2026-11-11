"""Live llm.delta frames over Redis: wire format, staleness gate, and the sender's transport and switches."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.live_deltas import DeltaPolicy, LiveDeltas
from surogates.session.events import EventType
from surogates.session.live import (
    DeltaGate,
    LiveFrame,
    Notification,
    decode_message,
    encode_live_frame,
)


def _message(data, kind="message"):
    return {"type": kind, "channel": b"surogates:session:x", "data": data}


def test_a_frame_round_trips_through_its_pub_sub_message():
    raw = encode_live_frame(41, 3, json.dumps({"content": "a:b"}))
    frame = decode_message(_message(raw.encode()))
    assert frame == LiveFrame(after=41, seq=3, data_json='{"content": "a:b"}')
    assert frame.data == {"content": "a:b"}


def test_an_events_notification_still_decodes_as_one():
    assert decode_message(_message(b"1234:llm.response")) == Notification(event_id=1234)
    assert decode_message(_message("77:session.complete", kind="pmessage")) == Notification(event_id=77)


@pytest.mark.parametrize("raw", [b"~x:0:{}", b"~1:{}", b"hello", b"", None])
def test_anything_else_is_no_frame_and_no_notification(raw):
    assert decode_message(_message(raw)) is None
    assert decode_message({"type": "subscribe", "data": 1}) is None


def test_the_gate_drops_frames_of_a_call_whose_end_was_passed_on():
    gate = DeltaGate()
    current = LiveFrame(after=10, seq=0, data_json="{}")
    assert gate.admits(current)
    gate.saw(EventType.LLM_REQUEST.value, 10)  # the call's own request: not its end
    assert gate.admits(current)
    gate.saw(EventType.TOOL_CALL.value, 12)  # a tool started mid-stream: not its end either
    gate.saw(EventType.LLM_HEARTBEAT.value, 13)
    assert gate.admits(current)
    gate.saw(EventType.LLM_RESPONSE.value, 15)
    assert not gate.admits(current)
    assert gate.admits(LiveFrame(after=15, seq=0, data_json="{}"))  # the next call (a summary after a response)


def test_a_floor_drops_the_frames_of_a_call_that_started_before_it():
    gate = DeltaGate(floor=20)
    assert not gate.admits(LiveFrame(after=19, seq=5, data_json="{}"))
    assert gate.admits(LiveFrame(after=21, seq=0, data_json="{}"))


def _sender(store, **policy):
    return LiveDeltas(store, uuid4(), policy=DeltaPolicy(**policy), iteration=2, turn_id="t", iteration_index=1)


async def test_over_redis_frames_are_published_and_no_event_is_written():
    store = AsyncMock()
    store.last_event_id.return_value = 99
    deltas = _sender(store, transport="redis", coalesce_ms=0)
    await deltas.text("reasoning", "think")
    await deltas.text("content", "Hi")
    await deltas.send({"reconnect": True})
    store.emit_event.assert_not_awaited()
    store.last_event_id.assert_awaited_once()  # once per call, not per frame
    sent = [(c.args[1], c.kwargs) for c in store.publish_live_frame.await_args_list]
    assert sent == [
        ({"reasoning": "think", "iteration": 2, "turn_id": "t", "iteration_index": 1}, {"after": 99, "seq": 0}),
        ({"content": "Hi", "iteration": 2, "turn_id": "t", "iteration_index": 1}, {"after": 99, "seq": 1}),
        ({"reconnect": True, "iteration": 2, "turn_id": "t", "iteration_index": 1}, {"after": 99, "seq": 2}),
    ]


async def test_to_the_log_deltas_are_events_as_before():
    store = AsyncMock()
    deltas = _sender(store, coalesce_ms=0)
    await deltas.text("content", "Hi")
    store.emit_event.assert_awaited_once()
    assert store.emit_event.await_args.args[1] == EventType.LLM_DELTA
    store.publish_live_frame.assert_not_awaited()


@pytest.mark.parametrize("transport", ["log", "redis"])
async def test_switched_off_a_call_sends_nothing_at_all(transport):
    store = AsyncMock()
    deltas = _sender(store, enabled=False, transport=transport)
    await deltas.text("content", "Hi")
    await deltas.reasoning_tokens(5)
    await deltas.send({"reconnect": True})
    await deltas.flush()
    store.emit_event.assert_not_awaited()
    store.publish_live_frame.assert_not_awaited()
    store.last_event_id.assert_not_awaited()


async def test_a_frame_that_cannot_be_published_does_not_fail_the_call():
    store = AsyncMock()
    store.last_event_id.side_effect = RuntimeError("database away")
    deltas = _sender(store, transport="redis", coalesce_ms=0)
    await deltas.text("content", "Hi")  # no exception
    store.publish_live_frame.assert_not_awaited()


def test_a_session_narrows_the_workers_policy():
    worker = DeltaPolicy(coalesce_ms=100, enabled=True, transport="redis")
    assert worker.for_session({}) == worker
    assert worker.for_session({"live_deltas": False}) == DeltaPolicy(coalesce_ms=100, enabled=False, transport="redis")
    assert worker.for_session({"delta_coalesce_ms": 0}).coalesce_ms == 0
    assert worker.for_session({"delta_coalesce_ms": "fast"}).coalesce_ms == 100
    # A session cannot turn back on what the worker turned off.
    assert not DeltaPolicy(enabled=False).for_session({"live_deltas": True}).enabled


def test_an_unknown_transport_is_refused():
    with pytest.raises(ValueError):
        DeltaPolicy(transport="kafka")


def test_the_worker_settings_default_to_the_log_and_read_the_environment(monkeypatch):
    from surogates.config import WorkerSettings

    settings = WorkerSettings()
    assert (settings.live_deltas, settings.delta_transport) == (True, "log")
    monkeypatch.setenv("SUROGATES_WORKER_LIVE_DELTAS", "false")
    monkeypatch.setenv("SUROGATES_WORKER_DELTA_TRANSPORT", "redis")
    settings = WorkerSettings()
    assert (settings.live_deltas, settings.delta_transport) == (False, "redis")
