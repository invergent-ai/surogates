"""Tests for retrying dropped streams with partial tool-call state."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import call_llm_with_retry
from surogates.session.events import EventType


class _FailingToolNameStream:
    """Yield a tool name, then fail before arguments are complete."""

    def __init__(self) -> None:
        self._idx = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx == 0:
            self._idx += 1
            tool_delta = SimpleNamespace(
                index=0,
                id="call_1",
                function=SimpleNamespace(name="write_file", arguments=""),
            )
            delta = SimpleNamespace(content=None, role=None, tool_calls=[tool_delta])
            return SimpleNamespace(
                choices=[SimpleNamespace(delta=delta, finish_reason=None)],
                model="gpt-4o",
                usage=None,
            )
        raise ConnectionError("network connection lost")

    async def aclose(self):
        self.closed = True


class _HealthyTextStream:
    def __init__(self) -> None:
        self._chunks = [
            self._chunk(content="recovered"),
            self._chunk(finish_reason="stop"),
        ]
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk

    @staticmethod
    def _chunk(content: str | None = None, finish_reason: str | None = None):
        delta = SimpleNamespace(content=content, role=None, tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
            model="gpt-4o",
            usage=None,
        )


@pytest.mark.asyncio
async def test_midstream_partial_tool_call_drop_retries_stream(monkeypatch):
    monkeypatch.setattr(
        "surogates.harness.llm_call.interruptible_sleep",
        AsyncMock(),
    )

    first_stream = _FailingToolNameStream()
    second_stream = _HealthyTextStream()
    llm_client = SimpleNamespace(
        base_url="https://api.openai.com/v1",
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(side_effect=[first_stream, second_stream])
            )
        ),
    )
    store = AsyncMock()

    assistant, usage = await call_llm_with_retry(
        session=SimpleNamespace(id=uuid4(), config={}, model="gpt-4o"),
        create_kwargs={"model": "gpt-4o", "messages": []},
        iteration=1,
        llm_client=llm_client,
        store=store,
        streaming_enabled=True,
        interrupt_check=lambda: False,
        rotate_credential=lambda *args, **kwargs: False,
        activate_fallback=lambda: False,
        get_current_model=lambda: "gpt-4o",
        set_streaming_enabled=lambda _enabled: None,
    )

    assert llm_client.chat.completions.create.await_count == 2
    assert first_stream.closed is True
    assert assistant["content"] == "recovered"
    assert usage["finish_reason"] == "stop"

    delta_events = [
        call.args
        for call in store.emit_event.await_args_list
        if call.args[1] == EventType.LLM_DELTA
    ]
    assert any(args[2].get("reconnect") is True for args in delta_events)
