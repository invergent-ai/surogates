"""LLM streaming watchdog cancellation and content scrubbing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import (
    call_llm_streaming_inner,
)


def _make_session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), config={"temperature": 0.7}, model="gpt-4o",
    )


class _BlockingStream:
    """Fake async stream that yields *some* chunks, then hangs forever.

    Simulates a connection where the provider sent a few tokens then
    stopped emitting anything (no StopAsyncIteration, no error).  The
    stream supports ``aclose`` so the watchdog can force the iterator
    to raise.
    """

    def __init__(self, prefix_chunks):
        self._prefix = list(prefix_chunks)
        self._i = 0
        self.closed = False
        self._close_event = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i < len(self._prefix):
            chunk = self._prefix[self._i]
            self._i += 1
            return chunk
        # Block until the stream is closed externally.  Mirrors what
        # happens when the upstream just... stops.
        await self._close_event.wait()
        raise StopAsyncIteration  # closed

    async def aclose(self):
        self.closed = True
        self._close_event.set()


@pytest.mark.asyncio
async def test_watchdog_closes_stale_stream(monkeypatch):
    """After STREAM_STALE_TIMEOUT of silence, the watchdog aborts the stream."""
    # Very short thresholds so the test doesn't wait 3 minutes.
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 0.5,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.1,
    )

    # One real chunk then indefinite silence.
    delta = SimpleNamespace(content="Hello", role="assistant", tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=None)
    chunk = SimpleNamespace(choices=[choice], model="gpt-4o", usage=None)
    stream = _BlockingStream([chunk])

    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)
    store = AsyncMock()

    msg, usage = await asyncio.wait_for(
        call_llm_streaming_inner(
            session=_make_session(),
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=lambda: False,
        ),
        # If the watchdog fails, we'll hit this overall timeout instead
        # and the test fails with a clear signal rather than hanging CI.
        timeout=3.0,
    )

    assert stream.closed is True, "watchdog must close the response on stall"
    assert usage["finish_reason"] == "interrupted"
    assert msg["content"] == "Hello"  # prefix content preserved


@pytest.mark.asyncio
async def test_watchdog_cancelled_cleanly_on_normal_completion(monkeypatch):
    """On a healthy stream the watchdog must exit without interfering."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 5.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.1,
    )

    # Three chunks, normal StopAsyncIteration at end.
    def _chunk(content=None, finish_reason=None):
        delta = SimpleNamespace(
            content=content, role=None, tool_calls=None,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
            model="gpt-4o", usage=None,
        )

    class _HealthyStream:
        def __init__(self, chunks):
            self._chunks = chunks
            self._i = 0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i >= len(self._chunks):
                raise StopAsyncIteration
            chunk = self._chunks[self._i]
            self._i += 1
            return chunk

        async def aclose(self):
            self.closed = True

    stream = _HealthyStream([
        _chunk(content="Hello"),
        _chunk(content=" world"),
        _chunk(finish_reason="stop"),
    ])

    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)
    store = AsyncMock()

    msg, usage = await call_llm_streaming_inner(
        session=_make_session(),
        create_kwargs={"model": "gpt-4o", "messages": []},
        iteration=1,
        llm_client=llm_client,
        store=store,
        interrupt_check=lambda: False,
    )

    assert stream.closed is False  # healthy stream doesn't trigger close
    assert usage["finish_reason"] == "stop"
    assert msg["content"] == "Hello world"


@pytest.mark.asyncio
async def test_streaming_scrubs_split_think_and_memory_context_tags(monkeypatch):
    """Split reasoning and memory-context tags are not emitted or persisted."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 5.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.1,
    )

    def _chunk(content=None, finish_reason=None):
        delta = SimpleNamespace(content=content, role=None, tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
            model="gpt-4o",
            usage=None,
        )

    class _Stream:
        def __init__(self, chunks):
            self._chunks = chunks
            self._i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i >= len(self._chunks):
                raise StopAsyncIteration
            chunk = self._chunks[self._i]
            self._i += 1
            return chunk

    stream = _Stream(
        [
            _chunk("Visible\n<th"),
            _chunk("ink>hidden reasoning</think>\n"),
            _chunk("Still visible <memory-"),
            _chunk("context>hidden memory</memory-context> done"),
            _chunk(finish_reason="stop"),
        ]
    )

    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)
    store = AsyncMock()

    msg, usage = await call_llm_streaming_inner(
        session=_make_session(),
        create_kwargs={"model": "gpt-4o", "messages": []},
        iteration=1,
        llm_client=llm_client,
        store=store,
        interrupt_check=lambda: False,
    )

    assert usage["finish_reason"] == "stop"
    assert msg["content"] == "Visible\n\nStill visible  done"
    emitted = "".join(
        call.args[2].get("content", "")
        for call in store.emit_event.await_args_list
    )
    assert emitted == msg["content"]
    assert "hidden reasoning" not in emitted
    assert "hidden memory" not in emitted


# -- A provider that never answers ------------------------------------------
# PROD 2026-11-09: one llm.buzz request held a turn 6 minutes before the
# response even started, then 600s more in the non-streaming fallback.


def _never_answers():
    async def _hang(**_kwargs):
        await asyncio.Event().wait()
    return _hang


def _gateway_524():
    import httpx
    from openai import APIStatusError

    req = httpx.Request("POST", "https://gw.example/v1/chat/completions")
    return APIStatusError(
        "<html>524: A timeout occurred</html>",
        response=httpx.Response(524, request=req),
        body=None,
    )


@pytest.mark.asyncio
async def test_unanswered_request_raises_stall(monkeypatch):
    from surogates.harness.llm_call import StreamStalledError

    monkeypatch.setattr("surogates.harness.llm_call.STREAM_FIRST_BYTE_TIMEOUT", 0.3)
    monkeypatch.setattr("surogates.harness.llm_call.NON_STREAMING_INTERRUPT_POLL", 0.05)
    llm_client = MagicMock()
    llm_client.chat.completions.create = _never_answers()

    with pytest.raises(StreamStalledError):
        await asyncio.wait_for(
            call_llm_streaming_inner(
                session=_make_session(),
                create_kwargs={"model": "gpt-4o", "messages": []},
                iteration=1, llm_client=llm_client, store=AsyncMock(),
                interrupt_check=lambda: False,
            ),
            timeout=3.0,
        )


@pytest.mark.asyncio
async def test_stop_while_waiting_for_the_provider(monkeypatch):
    monkeypatch.setattr("surogates.harness.llm_call.NON_STREAMING_INTERRUPT_POLL", 0.05)
    llm_client = MagicMock()
    llm_client.chat.completions.create = _never_answers()
    stopped = {"now": False}

    async def _stop_soon():
        await asyncio.sleep(0.2)
        stopped["now"] = True

    stopper = asyncio.ensure_future(_stop_soon())
    _msg, usage = await asyncio.wait_for(
        call_llm_streaming_inner(
            session=_make_session(),
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1, llm_client=llm_client, store=AsyncMock(),
            interrupt_check=lambda: stopped["now"],
        ),
        timeout=3.0,
    )
    await stopper
    assert usage["finish_reason"] == "interrupted"


@pytest.mark.asyncio
async def test_silent_stream_with_nothing_said_raises_stall(monkeypatch):
    from surogates.harness.llm_call import StreamStalledError

    monkeypatch.setattr("surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 0.3)
    monkeypatch.setattr("surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.05)
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=_BlockingStream([]))

    with pytest.raises(StreamStalledError):
        await asyncio.wait_for(
            call_llm_streaming_inner(
                session=_make_session(),
                create_kwargs={"model": "gpt-4o", "messages": []},
                iteration=1, llm_client=llm_client, store=AsyncMock(),
                interrupt_check=lambda: False,
            ),
            timeout=3.0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stall", "524"])
async def test_stuck_provider_is_retried_as_a_stream(monkeypatch, failure):
    """No detour through the non-streaming fallback: retry the stream."""
    from surogates.harness import llm_call
    from surogates.harness.llm_call import StreamStalledError, call_llm_with_retry

    good = ({"role": "assistant", "content": "gata"}, {"finish_reason": "stop"})
    first = StreamStalledError("stuck") if failure == "stall" else _gateway_524()
    inner = AsyncMock(side_effect=[first, good])
    non_streaming = AsyncMock()
    monkeypatch.setattr(llm_call, "call_llm_streaming_inner", inner)
    monkeypatch.setattr(llm_call, "call_llm_non_streaming", non_streaming)
    monkeypatch.setattr(llm_call, "jittered_backoff", lambda _attempt: 0)
    streaming = []

    msg, _usage = await call_llm_with_retry(
        session=_make_session(),
        create_kwargs={"model": "gpt-4o", "messages": []},
        iteration=1, llm_client=MagicMock(), store=AsyncMock(),
        streaming_enabled=True, interrupt_check=lambda: False,
        activate_fallback=lambda: False, get_current_model=lambda: None,
        set_streaming_enabled=streaming.append,
    )

    assert msg["content"] == "gata"
    assert inner.await_count == 2
    non_streaming.assert_not_awaited()
    assert streaming == []


@pytest.mark.asyncio
async def test_local_upstream_has_no_first_byte_bound(monkeypatch):
    """Ollama cold-loading a model must not be cut off."""
    monkeypatch.setattr("surogates.harness.llm_call.STREAM_FIRST_BYTE_TIMEOUT", 0.1)
    monkeypatch.setattr("surogates.harness.llm_call.NON_STREAMING_INTERRUPT_POLL", 0.05)

    async def _slow(**_kwargs):
        await asyncio.sleep(0.4)
        return _BlockingStream([])

    llm_client = MagicMock(base_url="http://localhost:11434/v1")
    llm_client.chat.completions.create = _slow
    stream_task = asyncio.ensure_future(call_llm_streaming_inner(
        session=_make_session(),
        create_kwargs={"model": "gpt-4o", "messages": []},
        iteration=1, llm_client=llm_client, store=AsyncMock(),
        interrupt_check=lambda: False,
    ))
    await asyncio.sleep(0.6)
    assert not stream_task.done(), "past the bound, still waiting on the local model"
    stream_task.cancel()


@pytest.mark.asyncio
async def test_reasoning_then_silence_is_not_restarted(monkeypatch):
    monkeypatch.setattr("surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 0.3)
    monkeypatch.setattr("surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.05)
    delta = SimpleNamespace(content=None, role="assistant", tool_calls=None,
                            reasoning_content="Mă gândesc")
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)],
                            model="gpt-4o", usage=None)
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=_BlockingStream([chunk]))

    _msg, usage = await asyncio.wait_for(
        call_llm_streaming_inner(
            session=_make_session(),
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1, llm_client=llm_client, store=AsyncMock(),
            interrupt_check=lambda: False,
        ),
        timeout=3.0,
    )
    assert usage["finish_reason"] == "interrupted"
