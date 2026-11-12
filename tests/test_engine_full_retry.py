"""A model call that finds the engine full is waited out, not failed (bench finding F26).

An engine with no KV room makes a request wait for admission, then gives up on it: a streamed request gets
an error event ("inference request expired while waiting for admission", code ``request_queue_timeout``) in
the stream it opened with HTTP 200, a non-streamed one an HTTP 503 with the same body.  The SDK raises the
first as an ``APIError`` with no status, so the harness took it for a provider that cannot stream: it switched
the session to non-streaming for good, waited in the queue a second time, and after three such attempts
(seconds apart) failed the turn.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from openai import APIError, APIStatusError, AsyncOpenAI

from surogates.harness import llm_call
from surogates.harness.llm_call import (
    MAX_ENGINE_FULL_RETRIES,
    MAX_LLM_RETRIES,
    call_llm_with_retry,
    is_engine_full,
)

EXPIRED = {
    "code": "request_queue_timeout", "message": "inference request expired while waiting for admission",
    "param": None, "type": "server_error",
}
GOOD = ({"role": "assistant", "content": "gata"}, {"finish_reason": "stop"})


def _status_error(status: int, body: dict | None = None, headers: dict | None = None) -> APIStatusError:
    request = httpx.Request("POST", "http://engine.test/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {})
    return APIStatusError(f"Error code: {status}", response=response, body=body)


def _stream_error() -> APIError:
    """What the SDK raises for an error event in a stream: no status."""
    return APIError(EXPIRED["message"], request=httpx.Request("POST", "http://engine.test/v1"), body=EXPIRED)


def test_what_counts_as_an_engine_with_no_room():
    assert is_engine_full(_stream_error())
    assert is_engine_full(_status_error(503, EXPIRED))
    assert is_engine_full(_status_error(503, {"error": EXPIRED}))
    assert is_engine_full(_status_error(503, headers={"retry-after": "7"}))
    assert is_engine_full(_status_error(529, headers={"retry-after": "7"}))
    # A 503 or 529 without either, any other 5xx, and an error the stream gives for other reasons are not.
    assert not is_engine_full(_status_error(503))
    assert not is_engine_full(_status_error(502, headers={"retry-after": "7"}))
    assert not is_engine_full(_status_error(500, {"message": "boom", "type": "server_error"}))
    assert not is_engine_full(APIError("Network connection lost.", request=httpx.Request("POST", "http://x"), body=None))


def _session():
    return SimpleNamespace(id=uuid4(), config={})


async def _call(inner_effects, monkeypatch, *, interrupt_check=lambda: False, fallback=lambda: False):
    inner = AsyncMock(side_effect=inner_effects)
    non_streaming = AsyncMock(side_effect=AssertionError("no non-streaming fallback"))
    waits: list[float] = []

    async def sleep(seconds, _interrupt_check):
        waits.append(seconds)

    monkeypatch.setattr(llm_call, "call_llm_streaming_inner", inner)
    monkeypatch.setattr(llm_call, "call_llm_non_streaming", non_streaming)
    monkeypatch.setattr(llm_call, "interruptible_sleep", sleep)
    streaming: list[bool] = []
    result = await call_llm_with_retry(
        session=_session(), create_kwargs={"model": "m", "messages": []}, iteration=1,
        llm_client=MagicMock(), store=AsyncMock(), streaming_enabled=True, interrupt_check=interrupt_check,
        activate_fallback=fallback, get_current_model=lambda: None, set_streaming_enabled=streaming.append,
    )
    return result, inner, waits, streaming


async def test_a_full_engine_is_waited_out_past_the_calls_own_attempts(monkeypatch):
    failures = MAX_LLM_RETRIES + 2
    (msg, _), inner, waits, streaming = await _call([_stream_error()] * failures + [GOOD], monkeypatch)
    assert msg["content"] == "gata"
    assert inner.await_count == failures + 1
    # Still a stream: the session is not switched to non-streaming.
    assert streaming == []
    # Backoff that grows: 5 s doubling, plus up to half again of jitter.
    assert len(waits) == failures
    assert all(5 * 2**i <= w <= 7.5 * 2**i for i, w in enumerate(waits[:3])), waits


async def test_the_engines_retry_after_is_honoured(monkeypatch):
    full = _status_error(503, headers={"retry-after": "7"})
    _, _, waits, _ = await _call([full, GOOD], monkeypatch)
    assert waits == [7.0]


async def test_the_wait_is_bounded(monkeypatch):
    attempts = MAX_ENGINE_FULL_RETRIES + MAX_LLM_RETRIES
    with pytest.raises(APIError):
        await _call([_stream_error()] * attempts + [GOOD], monkeypatch)
    inner = llm_call.call_llm_streaming_inner
    assert inner.await_count == attempts


async def test_a_provider_503_with_a_retry_after_gets_the_engine_full_budget(monkeypatch):
    full = _status_error(503, headers={"retry-after": "2"})
    (msg, _), inner, waits, _ = await _call([full] * (MAX_LLM_RETRIES + 1) + [GOOD], monkeypatch)
    assert msg["content"] == "gata"
    assert waits == [2.0] * (MAX_LLM_RETRIES + 1)


@pytest.mark.parametrize("value", ["-3", "nan", "inf", "0", "soon"])
def test_a_retry_after_that_is_no_wait_is_ignored(value):
    assert llm_call.extract_retry_after(_status_error(503, headers={"retry-after": value})) is None
    assert not is_engine_full(_status_error(503, headers={"retry-after": value}))


async def test_a_stop_during_the_wait_ends_it(monkeypatch):
    """With the real sleep: a stop while waiting for the engine ends the call, not the wait's end."""
    monkeypatch.setattr(llm_call, "ENGINE_FULL_BACKOFF_BASE", 30.0)
    inner = AsyncMock(side_effect=[_stream_error(), GOOD])
    monkeypatch.setattr(llm_call, "call_llm_streaming_inner", inner)
    stop_at = asyncio.get_running_loop().time() + 0.3
    msg, usage = await asyncio.wait_for(call_llm_with_retry(
        session=_session(), create_kwargs={"model": "m", "messages": []}, iteration=1,
        llm_client=MagicMock(), store=AsyncMock(), streaming_enabled=True,
        interrupt_check=lambda: asyncio.get_running_loop().time() > stop_at,
        activate_fallback=lambda: False, get_current_model=lambda: None, set_streaming_enabled=lambda _on: None,
    ), timeout=10)
    assert inner.await_count == 1
    assert usage["finish_reason"] == "interrupted"


async def test_past_the_bound_a_fallback_provider_is_still_tried(monkeypatch):
    switched = []

    def fallback():
        switched.append(True)
        return len(switched) == 1

    with pytest.raises(Exception):
        await _call([_stream_error()] * (MAX_ENGINE_FULL_RETRIES + MAX_LLM_RETRIES), monkeypatch, fallback=fallback)
    # The usual last resort is reached once the engine-full budget and the call's attempts are spent, as before.
    assert switched == [True]


async def test_a_stop_ends_the_wait(monkeypatch):
    checks = iter([False])  # running at the first attempt, stopped by the next
    (_msg, usage), inner, _, _ = await _call([_stream_error(), GOOD], monkeypatch, interrupt_check=lambda: next(checks, True))
    assert inner.await_count == 1
    assert usage["finish_reason"] == "interrupted"


async def test_a_full_engine_does_not_change_what_other_5xx_get(monkeypatch):
    plain = _status_error(503)
    with pytest.raises(APIStatusError):
        await _call([plain] * MAX_LLM_RETRIES + [GOOD], monkeypatch)
    assert llm_call.call_llm_streaming_inner.await_count == MAX_LLM_RETRIES


# -- End to end: the SDK over an engine that is full for a while -------------


class _FullEngine:
    """An OpenAI-compatible engine that expires the first *full* requests in its queue."""

    def __init__(self, full: int) -> None:
        self.full = full
        self.requests: list[bool] = []  # stream flag of each request

    def handle(self, request: httpx.Request) -> httpx.Response:
        stream = bool(json.loads(request.content).get("stream"))
        self.requests.append(stream)
        if len(self.requests) <= self.full:
            if stream:
                body = f"data: {json.dumps({'error': EXPIRED})}\n\n"
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())
            return httpx.Response(503, json={"error": EXPIRED})
        if stream:
            chunk = {
                "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "gata"}, "finish_reason": "stop"}],
            }
            body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "gata"}, "finish_reason": "stop"}],
        })


async def test_a_session_that_does_not_stream_waits_out_the_engines_503(monkeypatch):
    engine = _FullEngine(5)
    client = AsyncOpenAI(
        base_url="http://engine.test/v1", api_key="x", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(engine.handle)),
    )

    async def no_wait(_seconds, _interrupt_check):
        return None

    monkeypatch.setattr(llm_call, "interruptible_sleep", no_wait)
    msg, _usage = await call_llm_with_retry(
        session=_session(), create_kwargs={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        iteration=1, llm_client=client, store=AsyncMock(), streaming_enabled=False, interrupt_check=lambda: False,
        activate_fallback=lambda: False, get_current_model=lambda: None, set_streaming_enabled=lambda _on: None,
    )
    assert msg["content"] == "gata"
    assert engine.requests == [False] * 6


@pytest.mark.parametrize("full", [1, 4])
async def test_an_engine_full_for_a_while_answers_the_turn_as_a_stream(monkeypatch, full):
    engine = _FullEngine(full)
    client = AsyncOpenAI(
        base_url="http://engine.test/v1", api_key="x", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(engine.handle)),
    )

    async def no_wait(_seconds, _interrupt_check):
        return None

    monkeypatch.setattr(llm_call, "interruptible_sleep", no_wait)
    streaming: list[bool] = []
    msg, _usage = await call_llm_with_retry(
        session=_session(), create_kwargs={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        iteration=1, llm_client=client, store=AsyncMock(), streaming_enabled=True, interrupt_check=lambda: False,
        activate_fallback=lambda: False, get_current_model=lambda: None, set_streaming_enabled=streaming.append,
    )
    assert msg["content"] == "gata"
    # Every request was a stream: no detour through non-streaming, and the session keeps streaming.
    assert engine.requests == [True] * (full + 1)
    assert streaming == []
