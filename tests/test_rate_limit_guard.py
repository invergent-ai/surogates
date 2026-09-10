"""LLM requests reject provider cooldowns beyond the allowed wait."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.harness.llm_call import (
    MAX_RATE_LIMIT_WAIT_SECONDS,
    call_llm_with_retry,
)


@pytest.mark.asyncio
async def test_excessive_provider_cooldown_skips_provider_call() -> None:
    class ActiveGuard:
        async def remaining_seconds(self) -> float:
            return MAX_RATE_LIMIT_WAIT_SECONDS + 1

    create = AsyncMock()
    llm_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    with pytest.raises(RuntimeError, match="Provider is rate-limited"):
        await call_llm_with_retry(
            session=SimpleNamespace(id="session-1"),
            create_kwargs={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
            iteration=1,
            llm_client=llm_client,
            store=AsyncMock(),
            streaming_enabled=False,
            interrupt_check=lambda: False,
            activate_fallback=lambda: False,
            get_current_model=lambda: "test-model",
            set_streaming_enabled=lambda _enabled: None,
            rate_limit_guard=ActiveGuard(),
        )

    create.assert_not_called()
