"""Tests for the stream-stall watchdog in call_llm_streaming_inner.

Validates that when the upstream LLM stops sending bytes mid-stream
(common failure mode: proxy drops the connection without closing it)
the watchdog task notices after ``STREAM_STALE_TIMEOUT`` and closes
the response.  Without the watchdog the ``async for`` would block on
``__anext__()`` forever, holding the worker hostage on a dead stream.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import call_llm_streaming_inner


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


# Module-level marker so pytest-asyncio collects the module correctly.
pytestmark = pytest.mark.asyncio
