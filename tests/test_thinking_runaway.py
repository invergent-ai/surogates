"""Tests for the thinking-runaway mitigation: timeout bump, heartbeat,
in-stream runaway detection, and retry-with-thinking-off.

Each test exercises one layer in isolation; the runaway-retry test
(test_runaway_retry_disables_thinking) covers the end-to-end glue.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import (
    call_llm_streaming_inner,
    compute_stream_stale_timeout,
)
from surogates.session.events import EventType


def _make_session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        config={"temperature": 0.7},
        model="zai-org/GLM-5.1",
    )


# ---------------------------------------------------------------------------
# Task 2: conditional stale-timeout bump for reasoning models
# ---------------------------------------------------------------------------


def test_stream_stale_timeout_bumped_for_reasoning_models(monkeypatch):
    """Reasoning models get a 600s default so long silent reasoning
    phases on DeepInfra do not trip the watchdog."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 180.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", False,
    )

    timeout = compute_stream_stale_timeout(
        [{"role": "user", "content": "short request"}],
        base_url="https://api.deepinfra.com/v1/openai",
        model="zai-org/GLM-5.1",
    )

    assert timeout == 600.0


def test_stream_stale_timeout_unchanged_for_non_reasoning_models(monkeypatch):
    """OpenAI/Anthropic and other non-toggle models keep the 180s default."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 180.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", False,
    )

    timeout = compute_stream_stale_timeout(
        [{"role": "user", "content": "short request"}],
        base_url="https://api.openai.com/v1",
        model="gpt-4o",
    )

    assert timeout == 180.0


def test_stream_stale_timeout_explicit_override_wins_for_reasoning(monkeypatch):
    """SUROGATES_STREAM_STALE_TIMEOUT env var must override the reasoning bump."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 90.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )

    timeout = compute_stream_stale_timeout(
        [{"role": "user", "content": "short"}],
        base_url="https://api.deepinfra.com/v1/openai",
        model="zai-org/GLM-5.1",
        explicit_timeout=90.0,
    )

    assert timeout == 90.0


# ---------------------------------------------------------------------------
# Streaming-stream test fixtures shared by Tasks 3 and 4.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task 3: heartbeat emission from the watchdog
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task 4: in-stream runaway-reasoning detector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runaway_reasoning_cancels_stream(monkeypatch):
    """When reasoning_content chars exceed RUNAWAY_REASONING_CHAR_THRESHOLD
    without any content or tool_call delta, the stream is cancelled and
    the response is marked with stream_error_reason='runaway_reasoning'."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 30.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.RUNAWAY_REASONING_CHAR_THRESHOLD", 100,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.02,
    )

    # 6 reasoning chunks × 30 chars = 180 chars, crosses the 100 threshold.
    chunks = [_chunk(reasoning_content="x" * 30) for _ in range(6)]

    class _ReasoningStream(_BlockingStream):
        async def __anext__(self):
            if self._i < len(self._prefix):
                chunk = self._prefix[self._i]
                self._i += 1
                await asyncio.sleep(0.01)
                return chunk
            await self._close_event.wait()
            raise StopAsyncIteration

    stream = _ReasoningStream(chunks)
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
        timeout=3.0,
    )

    assert stream.closed is True
    assert usage["finish_reason"] == "interrupted"
    assert usage["stream_error_reason"] == "runaway_reasoning"


@pytest.mark.asyncio
async def test_runaway_detector_silent_after_content_arrives(monkeypatch):
    """Once any content delta has arrived, runaway detection MUST NOT
    fire even if reasoning continues to accumulate.  Some models
    interleave reasoning and content; we only care about the
    'all reasoning, never any visible output' failure mode."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 30.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.RUNAWAY_REASONING_CHAR_THRESHOLD", 50,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_CHUNK_POLL_INTERVAL", 0.02,
    )

    # Content arrives first, then a flood of reasoning -- not a runaway.
    chunks = [
        _chunk(content="Hello"),
        *[_chunk(reasoning_content="x" * 30) for _ in range(10)],
        _chunk(finish_reason="stop"),
    ]

    class _Stream:
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
            await asyncio.sleep(0.01)
            return chunk

        async def aclose(self):
            self.closed = True

    stream = _Stream(chunks)
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=stream)
    store = AsyncMock()

    msg, usage = await call_llm_streaming_inner(
        session=_make_session(),
        create_kwargs={"model": "zai-org/GLM-5.1", "messages": []},
        iteration=1,
        llm_client=llm_client,
        store=store,
        interrupt_check=lambda: False,
    )

    assert usage["finish_reason"] == "stop"
    assert usage.get("stream_error_reason") is None
    assert msg["content"] == "Hello"


# ---------------------------------------------------------------------------
# Task 5: per-turn thinking-disabled flag on AgentHarness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_gate_respects_disabled_flag(monkeypatch):
    """When _thinking_disabled_for_turn is True, the thinking gate
    forces enable_thinking=False regardless of the classifier."""
    from surogates.harness.loop import AgentHarness

    # Build a minimal AgentHarness without the full constructor by
    # directly instantiating and patching only what the gate uses.
    loop = AgentHarness.__new__(AgentHarness)
    loop._tenant = None
    loop._thinking_disabled_for_turn = True

    monkeypatch.setattr(
        "surogates.harness.loop.classify_hard_task_async",
        AsyncMock(side_effect=AssertionError("classifier should not be called")),
    )

    create_kwargs = {"model": "zai-org/GLM-5.1", "extra_body": {}}
    await loop._maybe_apply_thinking_gate(
        create_kwargs,
        messages=[{"role": "user", "content": "easy"}],
    )

    extra = create_kwargs["extra_body"]
    assert extra["chat_template_kwargs"]["enable_thinking"] is False


@pytest.mark.asyncio
async def test_thinking_gate_unchanged_when_flag_not_set(monkeypatch):
    """When flag is False and classifier says required=True, gate must
    leave extra_body alone (model default = thinking on)."""
    from surogates.harness.loop import AgentHarness

    loop = AgentHarness.__new__(AgentHarness)
    loop._tenant = None
    loop._thinking_disabled_for_turn = False

    monkeypatch.setattr(
        "surogates.harness.loop.classify_hard_task_async",
        AsyncMock(return_value=SimpleNamespace(
            required=True,
            category="debugging",
            reason="test",
        )),
    )

    create_kwargs = {"model": "zai-org/GLM-5.1"}
    await loop._maybe_apply_thinking_gate(
        create_kwargs,
        messages=[{"role": "user", "content": "Debug this Python stack trace and explain the root cause."}],
    )

    extra = create_kwargs.get("extra_body") or {}
    # Either no extra_body at all, or it doesn't disable thinking.
    if "chat_template_kwargs" in extra:
        assert extra["chat_template_kwargs"].get("enable_thinking") is not False
