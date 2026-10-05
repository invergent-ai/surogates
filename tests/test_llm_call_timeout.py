"""Slow non-streaming LLM requests emit heartbeats and surface provider failures."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from surogates.harness.llm_call import (
    call_llm_non_streaming,
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


@pytest.mark.asyncio
async def test_non_streaming_call_returns_at_once_when_the_turn_is_stopped() -> None:
    """The fallback after a failed stream used to ignore a stop: a phone caller who talked over the
    agent waited for the whole non-streaming completion (4+ s) before the turn ended."""
    stopped_at = None

    async def slow_create(**kwargs):
        await asyncio.sleep(10)
        return _fake_response()

    def interrupt_check() -> bool:
        return stopped_at is not None and time.monotonic() >= stopped_at

    client = SimpleNamespace(base_url=_PROXY_URL, chat=SimpleNamespace(completions=SimpleNamespace(create=slow_create)))
    stopped_at = time.monotonic() + 0.1
    started = time.monotonic()
    message, usage = await call_llm_non_streaming(
        session=_make_session(), create_kwargs={"model": "glm-5.2", "messages": []}, iteration=1,
        llm_client=client, store=SimpleNamespace(emit_event=AsyncMock()), turn_id="t1", iteration_index=0,
        interrupt_check=interrupt_check,
    )
    assert time.monotonic() - started < 1.0
    assert usage["finish_reason"] == "interrupted" and not message.get("content")


@pytest.mark.asyncio
async def test_a_stopped_turn_starts_no_new_llm_call() -> None:
    """After a backoff, the retry loop used to start a whole new call for a turn that was stopped."""
    from surogates.harness.llm_call import call_llm_with_retry

    create = AsyncMock(return_value=_fake_response())
    client = SimpleNamespace(base_url=_PROXY_URL, chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    message, usage = await call_llm_with_retry(
        session=_make_session(), create_kwargs={"model": "glm-5.2", "messages": []}, iteration=1, llm_client=client,
        store=SimpleNamespace(emit_event=AsyncMock()), streaming_enabled=False, interrupt_check=lambda: True,
        activate_fallback=lambda: False, get_current_model=lambda: None, set_streaming_enabled=lambda _: None,
    )
    assert usage["finish_reason"] == "interrupted" and create.await_count == 0
