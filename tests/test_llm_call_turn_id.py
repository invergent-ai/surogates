"""turn_id and iteration_index propagation into LLM_DELTA event payloads."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import call_llm_with_retry
from surogates.session.events import EventType


class _TextStream:
    """Emits two text deltas and a finish_reason=stop chunk."""

    def __init__(self) -> None:
        self._chunks = [
            self._chunk(content="hel"),
            self._chunk(content="lo"),
            self._chunk(finish_reason="stop"),
        ]
        self._idx = 0
        self.closed = False

    def __aiter__(self) -> "_TextStream":
        return self

    async def __anext__(self):
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True

    @staticmethod
    def _chunk(content: str | None = None, finish_reason: str | None = None):
        delta = SimpleNamespace(content=content, role=None, tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
            model="gpt-4o",
            usage=None,
        )


def _make_llm_client(stream):
    return SimpleNamespace(
        base_url="https://api.openai.com/v1",
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=stream))
        ),
    )


def test_call_llm_with_retry_signature_has_optional_turn_id() -> None:
    sig = inspect.signature(call_llm_with_retry)
    assert "turn_id" in sig.parameters
    param = sig.parameters["turn_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


@pytest.mark.asyncio
async def test_llm_delta_payloads_carry_turn_id_and_iteration_index() -> None:
    """Each LLM_DELTA emitted during streaming carries the supplied turn_id."""
    stream = _TextStream()
    llm_client = _make_llm_client(stream)
    store = AsyncMock()

    assistant, _usage = await call_llm_with_retry(
        session=SimpleNamespace(id=uuid4(), config={}, model="gpt-4o"),
        create_kwargs={"model": "gpt-4o", "messages": []},
        iteration=3,
        turn_id="turn-abc",
        llm_client=llm_client,
        store=store,
        streaming_enabled=True,
        interrupt_check=lambda: False,
        rotate_credential=lambda *args, **kwargs: False,
        activate_fallback=lambda: False,
        get_current_model=lambda: "gpt-4o",
        set_streaming_enabled=lambda _enabled: None,
    )
    assert assistant["content"] == "hello"

    delta_payloads = [
        call.args[2]
        for call in store.emit_event.await_args_list
        if call.args[1] == EventType.LLM_DELTA
    ]
    assert delta_payloads, "expected at least one LLM_DELTA emission"
    for payload in delta_payloads:
        assert payload["turn_id"] == "turn-abc"
        # iteration_index = iteration - 1 (0-based for SDK consumption).
        assert payload["iteration_index"] == 2


@pytest.mark.asyncio
async def test_llm_delta_payloads_omit_turn_id_when_not_supplied() -> None:
    """Older callers that pass no turn_id get the original payload shape."""
    stream = _TextStream()
    llm_client = _make_llm_client(stream)
    store = AsyncMock()

    await call_llm_with_retry(
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

    delta_payloads = [
        call.args[2]
        for call in store.emit_event.await_args_list
        if call.args[1] == EventType.LLM_DELTA
    ]
    assert delta_payloads
    for payload in delta_payloads:
        assert "turn_id" not in payload
        assert "iteration_index" not in payload
