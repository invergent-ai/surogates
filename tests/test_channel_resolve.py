"""Tests for surogates.channels.resolve.resolve_tenant."""

from __future__ import annotations

import pytest

from surogates.channels.resolve import resolve_tenant


class FakeCache:
    """Minimal fake cache that records get() calls."""

    def __init__(self, data: dict) -> None:
        self._data = data
        self.calls: list[str] = []

    async def get(self, key: str) -> dict | None:
        self.calls.append(key)
        return self._data.get(key)


@pytest.mark.asyncio
async def test_positive_returns_tenant_fields() -> None:
    """cache hit → org_id / agent_id / config returned."""
    routing = {
        "org_id": "org-abc",
        "agent_id": "agent-xyz",
        "api_web_url": "https://example.com",
        "config": {"some_flag": True},
    }
    cache = FakeCache({"slack:A0123ABCD": routing})

    result = await resolve_tenant(cache, "slack", "A0123ABCD")

    assert result is not None
    assert result["org_id"] == "org-abc"
    assert result["agent_id"] == "agent-xyz"
    assert result["api_web_url"] == "https://example.com"
    assert result["config"] == {"some_flag": True}


@pytest.mark.asyncio
async def test_positive_config_defaults_to_empty_dict() -> None:
    """cache hit but routing has no 'config' key → config defaults to {}."""
    routing = {
        "org_id": "org-abc",
        "agent_id": "agent-xyz",
        "api_web_url": "https://example.com",
    }
    cache = FakeCache({"telegram:@my_bot": routing})

    result = await resolve_tenant(cache, "telegram", "@my_bot")

    assert result is not None
    assert result["config"] == {}


@pytest.mark.asyncio
async def test_positive_config_none_coerced_to_empty_dict() -> None:
    """cache hit with config=None → config coerced to {}."""
    routing = {
        "org_id": "org-1",
        "agent_id": "agent-1",
        "config": None,
    }
    cache = FakeCache({"website:pk_abc": routing})

    result = await resolve_tenant(cache, "website", "pk_abc")

    assert result is not None
    assert result["config"] == {}


@pytest.mark.asyncio
async def test_negative_cache_miss_returns_none() -> None:
    """cache miss (cache.get returns None) → resolve_tenant returns None."""
    cache = FakeCache({})

    result = await resolve_tenant(cache, "slack", "UNKNOWN")

    assert result is None


@pytest.mark.asyncio
async def test_cache_get_called_once_with_correct_key() -> None:
    """cache.get is called exactly once with '<kind>:<identifier>'."""
    routing = {"org_id": "o", "agent_id": "a"}
    cache = FakeCache({"slack:A0001": routing})

    await resolve_tenant(cache, "slack", "A0001")

    assert cache.calls == ["slack:A0001"]


@pytest.mark.asyncio
async def test_cache_get_called_once_on_miss() -> None:
    """cache.get is called exactly once even on a miss."""
    cache = FakeCache({})

    await resolve_tenant(cache, "telegram", "@ghost_bot")

    assert cache.calls == ["telegram:@ghost_bot"]


@pytest.mark.asyncio
async def test_positive_api_web_url_absent_not_in_result() -> None:
    """api_web_url absent in routing → absent in result (no KeyError)."""
    routing = {"org_id": "o", "agent_id": "a"}
    cache = FakeCache({"slack:B9999": routing})

    result = await resolve_tenant(cache, "slack", "B9999")

    assert result is not None
    assert "api_web_url" not in result
