"""Reported reasoning usage reaches the client without text-based estimates."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import (
    _extract_reasoning_tokens,
    call_llm_streaming_inner,
)
from surogates.session.events import EventType


@pytest.mark.parametrize("details", [
    SimpleNamespace(reasoning_tokens=1200),
    {"reasoning_tokens": 1200},
])
def test_extract_reported_reasoning_usage(details):
    assert _extract_reasoning_tokens(
        SimpleNamespace(completion_tokens_details=details),
    ) == 1200


@pytest.mark.parametrize("value", [None, -1, True, "1200", 1.5])
def test_invalid_usage_does_not_become_a_count(value):
    assert _extract_reasoning_tokens(SimpleNamespace(
        completion_tokens_details={"reasoning_tokens": value},
    )) is None


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def aclose(self):
        pass


def chunk(reasoning=None, tokens=None):
    return SimpleNamespace(
        model="test-model",
        choices=[SimpleNamespace(
            delta=SimpleNamespace(reasoning=reasoning), finish_reason=None,
        )] if reasoning else [],
        usage=SimpleNamespace(
            completion_tokens_details=SimpleNamespace(reasoning_tokens=tokens),
        ) if tokens is not None else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [None, 0, 1200])
async def test_stream_forwards_only_provider_counts(reported):
    chunks = [chunk("A chunk containing several words. ")]
    if reported is not None:
        first_count = 100 if reported else 0
        chunks += [chunk("More reasoning. ", first_count), chunk(tokens=first_count), chunk(tokens=reported)]
    stream = Stream(chunks)
    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=AsyncMock(return_value=stream)),
    ))
    store = AsyncMock()
    _, usage = await call_llm_streaming_inner(
        session=SimpleNamespace(id=uuid4()),
        create_kwargs={"model": "test-model", "messages": []},
        iteration=2,
        turn_id="turn-1",
        llm_client=client,
        store=store,
    )
    counts = [
        call.args[2]
        for call in store.emit_event.await_args_list
        if call.args[1] == EventType.LLM_DELTA
        and "reasoning_tokens" in call.args[2]
    ]
    assert [payload["reasoning_tokens"] for payload in counts] == ([100, 1200] if reported else [])
    assert usage.get("reasoning_tokens") == (1200 if reported else None)
    assert usage["reasoning_delta_count"] == (1 if reported is None else 2)
    for payload in counts:
        assert payload["turn_id"] == "turn-1"
        assert payload["iteration_index"] == 1
