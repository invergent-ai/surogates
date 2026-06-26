"""Non-streaming LLM calls must stay observable and time-bounded.

Two regressions from the frozen-session incident:
- a non-streaming fallback inherited the AsyncOpenAI default timeout and
  could block for minutes -> give it an explicit, reasoning-aware
  per-request timeout (the SDK's own retries are left in place);
- a non-streaming call emits no events, so the UI looked frozen for the
  whole wait -> emit LLM_HEARTBEAT while the request is in flight.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from surogates.harness.llm_call import (
    NON_STREAMING_CONNECT_TIMEOUT,
    NON_STREAMING_POOL_TIMEOUT,
    NON_STREAMING_WRITE_TIMEOUT,
    STREAM_STALE_TIMEOUT,
    STREAM_STALE_TIMEOUT_REASONING,
    call_llm_non_streaming,
    compute_non_streaming_timeout,
)
from surogates.session.events import EventType

_PROXY_URL = "https://proxy.test/v1"


def _make_session():
    return SimpleNamespace(id=uuid4(), config={}, model="glm-5.2")


def _fake_response(model: str = "glm-5.2"):
    message = SimpleNamespace(role="assistant", content="hello")
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=usage, model=model)


def _fake_client(base_url: str = _PROXY_URL, response=None):
    create = AsyncMock(return_value=response or _fake_response())
    return SimpleNamespace(
        base_url=base_url,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ), create


# --- compute_non_streaming_timeout ----------------------------------------


def test_reasoning_model_gets_reasoning_ceiling() -> None:
    timeout = compute_non_streaming_timeout(
        {"model": "glm-5.2", "messages": []}, base_url=_PROXY_URL,
    )
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == STREAM_STALE_TIMEOUT_REASONING
    assert timeout.connect == NON_STREAMING_CONNECT_TIMEOUT
    assert timeout.write == NON_STREAMING_WRITE_TIMEOUT
    assert timeout.pool == NON_STREAMING_POOL_TIMEOUT


def test_plain_model_gets_base_ceiling() -> None:
    timeout = compute_non_streaming_timeout(
        {"model": "gpt-4o", "messages": []}, base_url=_PROXY_URL,
    )
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == STREAM_STALE_TIMEOUT


def test_local_upstream_is_uncapped() -> None:
    # A local model stays uncapped (mirrors the streaming watchdog), so the
    # caller leaves the client default in place instead of a finite bound.
    assert (
        compute_non_streaming_timeout(
            {"model": "glm-5.2", "messages": []},
            base_url="http://127.0.0.1:8000/v1",
        )
        is None
    )


# --- call_llm_non_streaming applies the timeout ---------------------------


@pytest.mark.asyncio
async def test_non_streaming_passes_reasoning_timeout() -> None:
    client, create = _fake_client()
    await call_llm_non_streaming(
        session=_make_session(),
        create_kwargs={"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
        iteration=1,
        llm_client=client,
    )
    timeout = create.await_args.kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == STREAM_STALE_TIMEOUT_REASONING


@pytest.mark.asyncio
async def test_non_streaming_local_omits_timeout() -> None:
    client, create = _fake_client(base_url="http://localhost:8000/v1")
    await call_llm_non_streaming(
        session=_make_session(),
        create_kwargs={"model": "glm-5.2", "messages": []},
        iteration=1,
        llm_client=client,
    )
    assert "timeout" not in create.await_args.kwargs


@pytest.mark.asyncio
async def test_non_streaming_timeout_propagates() -> None:
    # A timeout surfaces as the underlying error; the retry layer (and the
    # SDK's own retries, left in place) handle it.
    client, create = _fake_client()
    create.side_effect = httpx.ReadTimeout("stalled")
    with pytest.raises(httpx.ReadTimeout):
        await call_llm_non_streaming(
            session=_make_session(),
            create_kwargs={"model": "glm-5.2", "messages": []},
            iteration=1,
            llm_client=client,
        )


# --- non-streaming heartbeats keep the UI alive during the wait ----------


@pytest.mark.asyncio
async def test_non_streaming_emits_heartbeats(monkeypatch) -> None:
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_HEARTBEAT_INTERVAL", 0.02,
    )

    async def slow_create(**kwargs):
        await asyncio.sleep(0.07)
        return _fake_response()

    client = SimpleNamespace(
        base_url=_PROXY_URL,
        chat=SimpleNamespace(completions=SimpleNamespace(create=slow_create)),
    )
    store = SimpleNamespace(emit_event=AsyncMock())

    message, _usage = await call_llm_non_streaming(
        session=_make_session(),
        create_kwargs={"model": "glm-5.2", "messages": []},
        iteration=3,
        llm_client=client,
        store=store,
        turn_id="t1",
        iteration_index=2,
    )

    assert message["content"] == "hello"
    beats = [
        c for c in store.emit_event.await_args_list
        if c.args[1] == EventType.LLM_HEARTBEAT
    ]
    assert beats, "expected at least one heartbeat during the slow call"
    payload = beats[0].args[2]
    assert payload["iteration"] == 3
    assert payload["phase"] == "non_streaming"
    assert payload["turn_id"] == "t1"


@pytest.mark.asyncio
async def test_non_streaming_without_store_emits_nothing() -> None:
    client, _create = _fake_client()
    message, _usage = await call_llm_non_streaming(
        session=_make_session(),
        create_kwargs={"model": "glm-5.2", "messages": []},
        iteration=1,
        llm_client=client,
    )
    assert message["content"] == "hello"


@pytest.mark.asyncio
async def test_non_streaming_heartbeat_path_propagates_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_HEARTBEAT_INTERVAL", 0.02,
    )

    async def failing_create(**kwargs):
        await asyncio.sleep(0.03)
        raise httpx.ReadTimeout("stalled")

    client = SimpleNamespace(
        base_url=_PROXY_URL,
        chat=SimpleNamespace(completions=SimpleNamespace(create=failing_create)),
    )
    store = SimpleNamespace(emit_event=AsyncMock())

    with pytest.raises(httpx.ReadTimeout):
        await call_llm_non_streaming(
            session=_make_session(),
            create_kwargs={"model": "glm-5.2", "messages": []},
            iteration=1,
            llm_client=client,
            store=store,
        )
