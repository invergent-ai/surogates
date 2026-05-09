"""Tests for cross-session provider rate-limit guard."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.harness.llm_call import call_llm_with_retry
from surogates.harness.rate_limit_guard import ProviderRateLimitGuard


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value.encode("utf-8")

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.mark.asyncio
async def test_records_and_reads_remaining_rate_limit() -> None:
    redis = FakeRedis()
    guard = ProviderRateLimitGuard(redis, "https://api.provider.test/v1")

    await guard.record_until(time.time() + 120)

    remaining = await guard.remaining_seconds()
    assert remaining is not None
    assert 0 < remaining <= 120


@pytest.mark.asyncio
async def test_expired_rate_limit_is_cleared() -> None:
    redis = FakeRedis()
    guard = ProviderRateLimitGuard(redis, "openrouter")

    await guard.record_until(time.time() - 1)

    assert await guard.remaining_seconds() is None
    assert redis.values == {}


@pytest.mark.asyncio
async def test_missing_redis_is_noop() -> None:
    guard = ProviderRateLimitGuard(None, "openrouter")

    await guard.record_until(time.time() + 120)

    assert await guard.remaining_seconds() is None


@pytest.mark.asyncio
async def test_active_guard_skips_provider_call() -> None:
    class ActiveGuard:
        async def remaining_seconds(self) -> float:
            return 60.0

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
            rotate_credential=lambda *_args, **_kwargs: False,
            activate_fallback=lambda: False,
            get_current_model=lambda: "test-model",
            set_streaming_enabled=lambda _enabled: None,
            rate_limit_guard=ActiveGuard(),
        )

    create.assert_not_called()
