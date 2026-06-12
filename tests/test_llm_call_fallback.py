"""Streaming → non-streaming fallback must not mask the original error.

Regression for the production failure where the provider rejected the
conversation mid-stream with ``APIError: Repetitive tool calls detected``,
the automatic non-streaming fallback then got an empty response, and the
session failed with the generic ``Invalid LLM response: response is None
or has no choices`` — hiding the actionable provider error from the
crash-loop classifier, the event log, and the user.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import openai
import pytest

from surogates.harness.llm_call import call_llm_streaming


def _make_session():
    return SimpleNamespace(id=uuid4(), config={}, model="qwen3.7-max")


async def _call(streaming_exc: Exception, fallback: AsyncMock):
    with (
        patch(
            "surogates.harness.llm_call.call_llm_streaming_inner",
            AsyncMock(side_effect=streaming_exc),
        ),
        patch(
            "surogates.harness.llm_call.call_llm_non_streaming",
            fallback,
        ),
    ):
        return await call_llm_streaming(
            session=_make_session(),
            create_kwargs={"model": "qwen3.7-max", "messages": []},
            iteration=1,
            llm_client=SimpleNamespace(base_url="https://proxy.test/v1"),
            store=AsyncMock(),
            interrupt_check=lambda: False,
            set_streaming_enabled=lambda _enabled: None,
        )


@pytest.mark.asyncio
async def test_fallback_failure_raises_original_streaming_error() -> None:
    """When the non-streaming fallback also fails, the *streaming* error —
    which carries the real provider detail — must propagate, not the
    fallback's generic shape-validation error."""
    streaming_exc = openai.APIError(
        "Repetitive tool calls detected in the conversation history.",
        request=httpx.Request("POST", "https://proxy.test/v1/chat/completions"),
        body=None,
    )
    fallback = AsyncMock(
        side_effect=ValueError(
            "Invalid LLM response: response is None or has no choices"
        ),
    )

    with pytest.raises(openai.APIError, match="Repetitive tool calls"):
        await _call(streaming_exc, fallback)

    assert fallback.await_count == 1


@pytest.mark.asyncio
async def test_fallback_success_still_returns_result() -> None:
    """A successful fallback keeps the existing behaviour."""
    fallback = AsyncMock(return_value=({"role": "assistant", "content": "ok"}, {}))

    message, _usage = await _call(RuntimeError("stream broke"), fallback)

    assert message["content"] == "ok"
    assert fallback.await_count == 1
