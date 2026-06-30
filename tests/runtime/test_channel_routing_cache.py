"""Tests for ChannelRoutingCache.

Per-(channel_kind, channel_identifier) cache of
the routing record (org_id, agent_id, api_web_url) the inbound
handler needs.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_channel_routing_cache_hits_within_ttl():
    from surogates.runtime import ChannelRoutingCache

    fetches = 0

    async def loader(_):
        nonlocal fetches
        fetches += 1
        return {
            "org_id": "o-1", "agent_id": "a-1",
            "api_web_url": "https://web.acme",
        }

    cache = ChannelRoutingCache(loader=loader, ttl_seconds=10)
    assert (await cache.get("slack:A0123ABCD"))["agent_id"] == "a-1"
    assert (await cache.get("slack:A0123ABCD"))["agent_id"] == "a-1"
    assert fetches == 1


@pytest.mark.asyncio
async def test_max_entries_evicts_oldest_entry():
    """An opt-in size cap bounds memory for high-cardinality keyspaces (e.g. a
    per-sender identity cache): once full, the oldest entry is evicted."""
    from surogates.runtime import ChannelRoutingCache

    calls = []

    async def loader(key):
        calls.append(key)
        return {"k": key}

    cache = ChannelRoutingCache(loader=loader, ttl_seconds=100, max_entries=2)
    await cache.get("a")
    await cache.get("b")
    await cache.get("c")  # over cap → evict oldest ("a")
    assert calls == ["a", "b", "c"]

    await cache.get("c")  # hit
    await cache.get("b")  # hit
    assert calls == ["a", "b", "c"], "b and c still cached, no reload"

    await cache.get("a")  # was evicted → reload
    assert calls == ["a", "b", "c", "a"]


@pytest.mark.asyncio
async def test_set_seeds_value_so_get_skips_loader():
    """set() positively seeds a known value (e.g. a row just provisioned) so the
    next get is a hit instead of a reload."""
    from surogates.runtime import ChannelRoutingCache

    calls = []

    async def loader(key):
        calls.append(key)
        return {"loaded": True}

    cache = ChannelRoutingCache(loader=loader, ttl_seconds=100)
    cache.set("x", {"seeded": True})

    assert (await cache.get("x")) == {"seeded": True}
    assert calls == [], "seeded value served without calling the loader"


@pytest.mark.asyncio
async def test_channel_routing_cache_negative_memoised_until_ttl():
    """A lookup that resolves to None (no routing configured for
    this identifier) IS memoised — the SlugResolverCache
    established this convention so a malformed inbound event
    storm doesn't hammer the platform endpoint."""
    from surogates.runtime import ChannelRoutingCache

    fetches = 0

    async def loader(_):
        nonlocal fetches
        fetches += 1
        return None

    cache = ChannelRoutingCache(loader=loader, ttl_seconds=10)
    assert await cache.get("slack:unknown") is None
    assert await cache.get("slack:unknown") is None
    assert fetches == 1


@pytest.mark.asyncio
async def test_channel_routing_cache_invalidate_drops_entry():
    from surogates.runtime import ChannelRoutingCache

    fetches = 0

    async def loader(_):
        nonlocal fetches
        fetches += 1
        return {"org_id": "o-1", "agent_id": "a-1"}

    cache = ChannelRoutingCache(loader=loader, ttl_seconds=10)
    await cache.get("slack:A0123ABCD")
    cache.invalidate("slack:A0123ABCD")
    await cache.get("slack:A0123ABCD")
    assert fetches == 2


@pytest.mark.asyncio
async def test_channel_routing_cache_concurrent_dedup():
    from surogates.runtime import ChannelRoutingCache

    fetches = 0
    gate = asyncio.Event()

    async def loader(_):
        nonlocal fetches
        fetches += 1
        await gate.wait()
        return {"org_id": "o-1", "agent_id": "a-1"}

    cache = ChannelRoutingCache(loader=loader, ttl_seconds=10)
    tasks = [
        asyncio.create_task(cache.get("slack:A0123ABCD"))
        for _ in range(20)
    ]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    assert fetches == 1


@pytest.mark.asyncio
async def test_channel_routing_cache_loader_exception_not_memoised():
    from surogates.runtime import ChannelRoutingCache

    calls = 0

    async def loader(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("DB blip")
        return None

    cache = ChannelRoutingCache(loader=loader, ttl_seconds=10)
    with pytest.raises(RuntimeError):
        await cache.get("slack:A0123ABCD")
    await cache.get("slack:A0123ABCD")
    assert calls == 2
