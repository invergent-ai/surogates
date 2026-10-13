"""LLM heartbeat, and long reasoning left to finish."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import (
    call_llm_streaming_inner,
)
from surogates.session.events import EventType


def _make_session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        config={"temperature": 0.7},
        model="zai-org/GLM-5.1",
    )


class _BlockingStream:
    """Yields prefix chunks, then hangs until aclose()."""

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
        await self._close_event.wait()
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True
        self._close_event.set()


def _chunk(content=None, reasoning_content=None, finish_reason=None,
           tool_calls=None):
    delta = SimpleNamespace(
        content=content,
        role=None,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        model="zai-org/GLM-5.1",
        usage=None,
    )


@pytest.mark.asyncio
async def test_watchdog_emits_heartbeat_during_silent_stream(monkeypatch):
    """When the stream is silent past STREAM_HEARTBEAT_INTERVAL but
    still inside the stale_timeout window, the watchdog must emit
    LLM_HEARTBEAT events so the UI can show 'still working'."""
    # Compressed timings so the test runs in ~1s, not minutes.
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 2.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_HEARTBEAT_INTERVAL", 0.2,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.05,
    )

    stream = _BlockingStream([_chunk(content="Hi")])
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)
    store = AsyncMock()

    msg, usage = await asyncio.wait_for(
        call_llm_streaming_inner(
            session=_make_session(),
            create_kwargs={"model": "zai-org/GLM-5.1", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=lambda: False,
        ),
        timeout=5.0,
    )

    heartbeat_calls = [
        c for c in store.emit_event.await_args_list
        if c.args[1] == EventType.LLM_HEARTBEAT
    ]
    # In 2s stale window with 0.2s heartbeat interval, expect at least 3
    # heartbeats (roughly t=0.2, 0.4, 0.6, ... before stale at t=2.0).
    assert len(heartbeat_calls) >= 3, (
        f"expected ≥3 heartbeats, got {len(heartbeat_calls)}"
    )
    payload = heartbeat_calls[0].args[2]
    assert payload["iteration"] == 1


@pytest.mark.asyncio
async def test_long_reasoning_before_any_content_is_not_cut(monkeypatch):
    """A model may reason at length before it answers.  A character-count
    cut-off once cancelled such streams: on PROD it ended every one of 19
    cuts in 14 days mid-plan (contract review, revenue model, deck), none of
    them repetitive, and retried with a thinking switch the provider ignored.
    """
    monkeypatch.setattr("surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 30.0)
    monkeypatch.setattr("surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True)
    monkeypatch.setattr("surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.02)

    # 40 000 characters of reasoning, then the answer.
    chunks = [_chunk(reasoning_content="x" * 40) for _ in range(1000)]
    chunks.append(_chunk(content="done", finish_reason="stop"))

    class _FiniteStream(_BlockingStream):
        async def __anext__(self):
            if self._i < len(self._prefix):
                chunk = self._prefix[self._i]
                self._i += 1
                return chunk
            raise StopAsyncIteration

    stream = _FiniteStream(chunks)
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)

    msg, usage = await asyncio.wait_for(
        call_llm_streaming_inner(
            session=_make_session(),
            create_kwargs={"model": "deepseek/deepseek-v4.1-flash", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=AsyncMock(),
            interrupt_check=lambda: False,
        ),
        timeout=10.0,
    )

    assert msg["content"] == "done"
    assert usage["finish_reason"] == "stop"
    assert "stream_error_reason" not in usage
