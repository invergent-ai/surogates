"""A rate-limited LLM request continues with the next configured provider."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_a_rate_limit_moves_the_session_to_the_next_provider(monkeypatch):
    """Waiting out a retry-after is worse for the session than finishing
    it somewhere else, so a 429 fails over rather than sleeping."""
    from surogates.harness import llm_call

    calls: list[str] = []

    class _RateLimited(Exception):
        status_code = 429

    async def create(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise _RateLimited("rate limited")
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="done", tool_calls=None, role="assistant"),
                finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            model="second",
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        base_url="https://primary.example",
    )
    model = {"v": "main-model"}

    def activate_fallback() -> bool:
        model["v"] = "second"
        return True

    store = AsyncMock()
    store.emit_event = AsyncMock(return_value=1)

    await llm_call.call_llm_with_retry(
        session=SimpleNamespace(id="s", config={}),
        create_kwargs={"model": "main-model", "messages": []},
        iteration=1,
        llm_client=client,
        store=store,
        streaming_enabled=False,
        interrupt_check=lambda: False,
        activate_fallback=activate_fallback,
        get_current_model=lambda: model["v"],
        set_streaming_enabled=lambda _v: None,
    )

    assert calls == ["main-model", "second"]
