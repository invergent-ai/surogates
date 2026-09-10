"""Slow non-streaming LLM requests emit heartbeats and surface provider failures."""

from __future__ import annotations

import asyncio
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
