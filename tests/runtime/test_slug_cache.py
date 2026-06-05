"""Tests for SlugResolverCache.

Long-TTL (30s) cache mapping slug → agent_id.
Stores ``None`` entries too so a reserved-subdomain miss does not
re-query the management plane on every Host-header probe.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_slug_cache_hits_within_ttl():
    from surogates.runtime import SlugResolverCache

    fetches = 0

    async def loader(_slug: str) -> str | None:
        nonlocal fetches
        fetches += 1
        return "agent-x"

    cache = SlugResolverCache(loader=loader, ttl_seconds=10)
    assert (await cache.get("acme")) == "agent-x"
    assert (await cache.get("acme")) == "agent-x"
    assert fetches == 1


@pytest.mark.asyncio
async def test_slug_cache_memoises_misses():
    """A miss (loader returns None) is cached too so reserved-
    subdomain probes do not hit the management plane every time."""
    from surogates.runtime import SlugResolverCache

    fetches = 0

    async def loader(_slug: str) -> str | None:
        nonlocal fetches
        fetches += 1
        return None

    cache = SlugResolverCache(loader=loader, ttl_seconds=10)
    assert (await cache.get("nope")) is None
    assert (await cache.get("nope")) is None
    assert fetches == 1


@pytest.mark.asyncio
async def test_slug_cache_invalidate():
    from surogates.runtime import SlugResolverCache

    fetches = 0

    async def loader(_slug: str) -> str | None:
        nonlocal fetches
        fetches += 1
        return "x"

    cache = SlugResolverCache(loader=loader, ttl_seconds=10)
    assert (await cache.get("acme")) == "x"
    cache.invalidate("acme")
    assert (await cache.get("acme")) == "x"
    assert fetches == 2


@pytest.mark.asyncio
async def test_slug_cache_concurrent_calls_dedup_single_loader_call():
    """Per-key lock prevents thundering-herd against the platform API.

    20 concurrent gets for the same fresh slug must produce 1 loader
    call, not 20."""
    from surogates.runtime import SlugResolverCache

    fetches = 0
    gate = asyncio.Event()

    async def loader(_slug: str) -> str | None:
        nonlocal fetches
        fetches += 1
        await gate.wait()
        return "agent-x"

    cache = SlugResolverCache(loader=loader, ttl_seconds=10)
    tasks = [asyncio.create_task(cache.get("acme")) for _ in range(20)]
    await asyncio.sleep(0)  # let tasks register on the per-key lock
    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r == "agent-x" for r in results)
    assert fetches == 1


@pytest.mark.asyncio
async def test_slug_cache_loader_exception_not_memoised():
    """Unlike the None ``miss`` (a *known* answer the platform gave us),
    a loader exception is an *unknown* state — call N+1 must retry."""
    from surogates.runtime import SlugResolverCache

    calls = 0

    async def loader(_slug: str) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return "agent-x"

    cache = SlugResolverCache(loader=loader, ttl_seconds=10)
    with pytest.raises(RuntimeError):
        await cache.get("acme")
    assert (await cache.get("acme")) == "agent-x"
    assert calls == 2


def test_slug_cache_channel_registered_in_invalidation_channels():
    from surogates.runtime import INVALIDATION_CHANNELS

    assert "agent.slug_changed:" in INVALIDATION_CHANNELS
