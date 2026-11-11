"""Streamed ``llm.delta`` events are coalesced in time, and no text is lost or misordered.

Covers :mod:`surogates.harness.live_deltas` on its own (a controlled clock)
and through ``call_llm_streaming_inner`` / ``call_llm_with_retry`` (a scripted
provider stream): the first chunk goes out at once, chunks inside the window
join one event, a change of kind and the end of the stream (however it ends)
send what has gathered, and every delta of a call is written before the call
returns, so before the harness writes its ``llm.response``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.config import WorkerSettings
from surogates.harness import live_deltas as live_deltas_module
from surogates.harness.live_deltas import DEFAULT_COALESCE_MS, DeltaPolicy, LiveDeltas
from surogates.harness.llm_call import call_llm_streaming_inner, call_llm_with_retry
from surogates.session.events import EventType


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """The coalescing windows' clock only: the event loop keeps its own."""
    c = Clock()
    monkeypatch.setattr(live_deltas_module, "_clock", c)
    return c


def _deltas(store) -> list[dict]:
    return [
        call.args[2] for call in store.emit_event.await_args_list
        if call.args[1] == EventType.LLM_DELTA
    ]


def _writer(store, coalesce_ms=DEFAULT_COALESCE_MS, **meta) -> LiveDeltas:
    return LiveDeltas(store, uuid4(), policy=DeltaPolicy(coalesce_ms=coalesce_ms), iteration=3, **meta)


# ---------------------------------------------------------------------------
# LiveDeltas alone
# ---------------------------------------------------------------------------


async def test_the_first_chunk_goes_out_at_once_and_the_window_gathers_the_rest(clock):
    store = AsyncMock()
    deltas = _writer(store)
    await deltas.text("content", "Hel")
    assert _deltas(store) == [{"content": "Hel", "iteration": 3, "chunks": 1}]
    clock.now += 0.03
    await deltas.text("content", "lo")
    clock.now += 0.03
    await deltas.text("content", ", wor")
    assert len(_deltas(store)) == 1  # still inside the window
    clock.now += 0.05  # 110 ms after the first send: the window has closed
    await deltas.text("content", "ld")
    assert _deltas(store)[1:] == [{"content": "lo, world", "iteration": 3, "chunks": 3}]


async def test_a_tick_sends_what_gathered_once_the_window_closes(clock):
    store = AsyncMock()
    deltas = _writer(store)
    await deltas.text("content", "a")
    await deltas.text("content", "b")
    await deltas.tick()
    assert [d["content"] for d in _deltas(store)] == ["a"]
    clock.now += DEFAULT_COALESCE_MS / 1000
    await deltas.tick()  # a chunk with no text (a tool-call or usage chunk)
    assert [d["content"] for d in _deltas(store)] == ["a", "b"]


async def test_a_change_of_kind_sends_what_gathered_first(clock):
    store = AsyncMock()
    deltas = _writer(store)
    await deltas.text("reasoning", "think ")
    await deltas.text("reasoning", "more ")
    await deltas.text("content", "Answer")
    await deltas.text("content", " here")
    await deltas.flush()
    assert _deltas(store) == [
        {"reasoning": "think ", "iteration": 3, "chunks": 1, "reasoning_tokens": 1},
        {"reasoning": "more ", "iteration": 3, "chunks": 1, "reasoning_tokens": 2},
        {"content": "Answer here", "iteration": 3, "chunks": 2},
    ]


async def test_zero_writes_one_event_per_chunk(clock):
    store = AsyncMock()
    deltas = _writer(store, coalesce_ms=0)
    for piece in ("a", "b", "c"):
        await deltas.text("content", piece)
    await deltas.text("reasoning", "r")
    # The events releases before coalescing wrote, field for field.
    assert _deltas(store) == [
        {"content": "a", "iteration": 3},
        {"content": "b", "iteration": 3},
        {"content": "c", "iteration": 3},
        {"reasoning": "r", "iteration": 3},
    ]


async def test_the_reasoning_token_count_sends_its_newest_value(clock):
    store = AsyncMock()
    deltas = _writer(store)
    await deltas.text("reasoning", "x")
    await deltas.reasoning_tokens(10)
    await deltas.reasoning_tokens(20)
    await deltas.flush()
    assert _deltas(store) == [
        {"reasoning": "x", "iteration": 3, "chunks": 1, "reasoning_tokens": 1},
        {"reasoning_tokens": 20, "iteration": 3},
    ]


async def test_coalesced_reasoning_carries_the_count_a_viewer_kept_per_chunk(clock):
    """The web chat shows reasoning_tokens before its own count of reasoning events: each coalesced
    event carries the call's reasoning chunks so far, the number that count reached when every
    chunk was an event."""
    store = AsyncMock()
    deltas = _writer(store)
    for _ in range(4):
        await deltas.text("reasoning", "r")
    clock.now += 0.2
    await deltas.text("reasoning", "r")
    await deltas.text("reasoning", "r")
    await deltas.flush()
    events = _deltas(store)
    assert [(d["chunks"], d["reasoning_tokens"]) for d in events] == [(1, 1), (4, 5), (1, 6)]
    assert sum(d["chunks"] for d in events) == 6


async def test_once_the_provider_reports_a_count_reasoning_events_carry_it(clock):
    store = AsyncMock()
    deltas = _writer(store)
    await deltas.text("reasoning", "r")
    await deltas.reasoning_tokens(250)
    await deltas.text("reasoning", "s")
    await deltas.flush()
    assert _deltas(store) == [
        {"reasoning": "r", "iteration": 3, "chunks": 1, "reasoning_tokens": 1},
        {"reasoning": "s", "iteration": 3, "chunks": 1, "reasoning_tokens": 250},
        {"reasoning_tokens": 250, "iteration": 3},
    ]


async def test_an_event_of_its_own_follows_what_gathered(clock):
    store = AsyncMock()
    deltas = _writer(store)
    await deltas.text("content", "a")
    await deltas.text("content", "partial")
    await deltas.send({"reconnect": True, "iteration": 3})
    assert _deltas(store) == [
        {"content": "a", "iteration": 3, "chunks": 1},
        {"content": "partial", "iteration": 3, "chunks": 1},
        {"reconnect": True, "iteration": 3},
    ]


async def test_turn_meta_is_stamped_on_every_event(clock):
    store = AsyncMock()
    deltas = _writer(store, turn_id="turn-7", iteration_index=4)
    await deltas.text("content", "a")
    await deltas.text("content", "b")
    await deltas.flush()
    assert _deltas(store) == [
        {"content": "a", "iteration": 3, "chunks": 1, "turn_id": "turn-7", "iteration_index": 4},
        {"content": "b", "iteration": 3, "chunks": 1, "turn_id": "turn-7", "iteration_index": 4},
    ]


def test_the_worker_setting_defaults_to_100_ms_and_reads_the_environment(monkeypatch):
    assert WorkerSettings().delta_coalesce_ms == DEFAULT_COALESCE_MS == 100
    monkeypatch.setenv("SUROGATES_WORKER_DELTA_COALESCE_MS", "0")
    assert WorkerSettings().delta_coalesce_ms == 0


# ---------------------------------------------------------------------------
# Through the streaming call
# ---------------------------------------------------------------------------


class Stream:
    """A provider stream: *items* are chunks, or an exception to raise, or a
    callable run when it is reached (to move the clock, say), or a number of
    seconds to stay silent."""

    def __init__(self, items) -> None:
        self.items = iter(items)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            try:
                item = next(self.items)
            except StopIteration:
                raise StopAsyncIteration
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, float):
                await asyncio.sleep(item)
                continue
            if callable(item):
                item()
                continue
            return item

    async def aclose(self):
        self.closed = True


def _chunk(content=None, reasoning=None, tool_calls=None, finish_reason=None):
    return SimpleNamespace(
        model="m",
        usage=None,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content, reasoning=reasoning, tool_calls=tool_calls, role=None),
            finish_reason=finish_reason,
        )],
    )


def _client(*streams):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=list(streams)),
    )))


class Recorder:
    """A store whose ``emit_event`` records each event in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit_event(self, session_id, event_type, data, **kwargs):
        self.events.append((event_type.value, data))
        return len(self.events)

    def deltas(self) -> list[dict]:
        return [data for kind, data in self.events if kind == EventType.LLM_DELTA.value]


async def _stream_call(store, stream, *, coalesce_ms=DEFAULT_COALESCE_MS, **kwargs):
    session = SimpleNamespace(id=uuid4())
    return await call_llm_streaming_inner(
        session=session,
        create_kwargs={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        iteration=1,
        llm_client=_client(stream),
        store=store,
        live_deltas=LiveDeltas(store, session.id, policy=DeltaPolicy(coalesce_ms=coalesce_ms), iteration=1),
        **kwargs,
    )


async def test_the_stream_end_sends_the_tail_and_no_text_is_lost(clock):
    store = Recorder()
    pieces = ["The ", "quick ", "brown ", "fox ", "jumps."]
    message, usage = await _stream_call(store, Stream([_chunk(content=p) for p in pieces] + [_chunk(finish_reason="stop")]))
    deltas = store.deltas()
    # One chunk at once, the rest of the (instant) stream in one event at its end.
    assert [d["content"] for d in deltas] == ["The ", "quick brown fox jumps."]
    assert "".join(d["content"] for d in deltas) == message["content"] == "".join(pieces)


async def test_without_coalescing_every_chunk_is_an_event(clock):
    store = Recorder()
    pieces = ["a", "b", "c"]
    await _stream_call(store, Stream([_chunk(content=p) for p in pieces]), coalesce_ms=0)
    assert [d["content"] for d in store.deltas()] == pieces


async def test_chunks_spread_over_time_leave_once_per_window(clock):
    store = Recorder()

    def later():
        clock.now += 0.04  # 40 ms between chunks: ~2.5 chunks per window

    items = []
    for i in range(10):
        items += [later, _chunk(content=f"{i} ")]
    message, _ = await _stream_call(store, Stream(items))
    deltas = store.deltas()
    assert 3 <= len(deltas) <= 5 < 10
    assert "".join(d["content"] for d in deltas) == message["content"]


async def test_reasoning_then_content_keeps_its_order(clock):
    store = Recorder()
    items = [_chunk(reasoning="r1 "), _chunk(reasoning="r2 "), _chunk(content="c1 "), _chunk(content="c2")]
    message, _ = await _stream_call(store, Stream(items))
    assert store.deltas() == [
        {"reasoning": "r1 ", "iteration": 1, "chunks": 1, "reasoning_tokens": 1},
        {"reasoning": "r2 ", "iteration": 1, "chunks": 1, "reasoning_tokens": 2},
        {"content": "c1 c2", "iteration": 1, "chunks": 2},
    ]
    assert message["reasoning_content"] == "r1 r2 "


async def test_a_stream_that_fails_still_sends_what_gathered(clock):
    store = Recorder()
    items = [_chunk(content="a"), _chunk(content="b"), _chunk(content="c"), RuntimeError("upstream dropped")]
    with pytest.raises(RuntimeError, match="upstream dropped"):
        await _stream_call(store, Stream(items))
    assert "".join(d["content"] for d in store.deltas()) == "abc"


async def test_an_interrupted_stream_still_sends_what_gathered(clock):
    store = Recorder()
    stop = {"now": False}

    def press_stop():
        stop["now"] = True

    items = [_chunk(content="a"), _chunk(content="b"), press_stop, _chunk(content="never")]
    message, usage = await _stream_call(store, Stream(items), interrupt_check=lambda: stop["now"])
    assert usage["finish_reason"] == "interrupted"
    assert "".join(d["content"] for d in store.deltas()) == message["content"] == "ab"


async def test_every_delta_of_a_call_lands_before_its_response(clock):
    """The harness writes ``llm.response`` after ``call_llm_with_retry`` returns: nothing of the call may follow it."""
    store = Recorder()
    session = SimpleNamespace(id=uuid4(), config={})
    items = [_chunk(content=p) for p in ("one ", "two ", "three")]
    message, _ = await call_llm_with_retry(
        session=session,
        create_kwargs={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        iteration=1,
        llm_client=_client(Stream(items)),
        store=store,
        streaming_enabled=True,
        interrupt_check=lambda: False,
        activate_fallback=lambda: False,
        get_current_model=lambda: "m",
        set_streaming_enabled=lambda enabled: None,
        delta_policy=DeltaPolicy(),
    )
    await store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": message})
    kinds = [kind for kind, _ in store.events]
    assert kinds[-1] == EventType.LLM_RESPONSE.value
    assert EventType.LLM_DELTA.value not in kinds[kinds.index(EventType.LLM_RESPONSE.value):]
    assert "".join(d["content"] for d in store.deltas()) == message["content"] == "one two three"


async def test_a_partial_tool_call_retry_sends_the_partial_text_before_its_reconnect(clock, monkeypatch):
    monkeypatch.setattr("surogates.harness.llm_call.jittered_backoff", lambda attempt: 0)
    store = Recorder()
    session = SimpleNamespace(id=uuid4(), config={})
    tool_delta = SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="read_file", arguments='{"pa'))
    first = Stream([_chunk(content="Let "), _chunk(content="me look"), _chunk(tool_calls=[tool_delta]),
                    RuntimeError("connection reset")])
    second = Stream([_chunk(content="Done.")])
    message, _ = await call_llm_with_retry(
        session=session,
        create_kwargs={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        iteration=1,
        llm_client=_client(first, second),
        store=store,
        streaming_enabled=True,
        interrupt_check=lambda: False,
        activate_fallback=lambda: False,
        get_current_model=lambda: "m",
        set_streaming_enabled=lambda enabled: None,
        delta_policy=DeltaPolicy(),
    )
    deltas = store.deltas()
    reconnect = next(i for i, d in enumerate(deltas) if d.get("reconnect"))
    assert "".join(d.get("content", "") for d in deltas[:reconnect]) == "Let me look"
    assert "".join(d.get("content", "") for d in deltas[reconnect + 1:]) == message["content"] == "Done."


async def test_text_held_while_the_stream_goes_quiet_is_written_when_its_window_closes():
    """No clock fixture: the window's timer runs on the event loop."""
    store = Recorder()
    seen_before_c: list[str] = []
    items = [_chunk(content="a"), _chunk(content="b"), 0.3,
             lambda: seen_before_c.extend(d["content"] for d in store.deltas()), _chunk(content="c")]
    message, _ = await _stream_call(store, Stream(items), coalesce_ms=30)
    assert seen_before_c == ["a", "b"]  # "b" did not wait 300 ms for "c"
    assert "".join(d["content"] for d in store.deltas()) == message["content"] == "abc"


async def test_the_text_before_a_tool_call_is_written_before_the_call_can_start(clock):
    store = Recorder()
    at_start: list[str] = []

    def tool(index, name):
        return _chunk(tool_calls=[SimpleNamespace(index=index, id=f"call_{index}",
                                                  function=SimpleNamespace(name=name, arguments="{}"))])

    items = [_chunk(content="Let me "), _chunk(content="look it up."), tool(0, "search"), tool(1, "read")]
    await _stream_call(store, Stream(items),
                       on_tool_call_complete=lambda entry: at_start.append("".join(d["content"] for d in store.deltas())))
    assert at_start == ["Let me look it up.", "Let me look it up."]
