"""Tests for ``surogates.runtime.RuntimeConfigCache``.

In-process TTL cache fronting the PlatformClient.
The cache is a pure cache — the platform PG is the source of truth.
Entries are evicted by TTL or by explicit ``invalidate(agent_id)``.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_cache_hits_within_ttl():
    from surogates.runtime import RuntimeConfigCache

    fetches = 0

    async def loader(agent_id: str) -> dict:
        nonlocal fetches
        fetches += 1
        return {"agent_id": agent_id, "v": 1}

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    assert (await cache.get("a-1"))["v"] == 1
    assert (await cache.get("a-1"))["v"] == 1
    assert fetches == 1


@pytest.mark.asyncio
async def test_cache_refetches_after_ttl():
    from surogates.runtime import RuntimeConfigCache

    fetches = 0

    async def loader(agent_id: str) -> dict:
        nonlocal fetches
        fetches += 1
        return {"agent_id": agent_id, "v": fetches}

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=0.05)
    await cache.get("a-1")
    await asyncio.sleep(0.1)
    await cache.get("a-1")
    assert fetches == 2


@pytest.mark.asyncio
async def test_cache_invalidate_drops_entry():
    from surogates.runtime import RuntimeConfigCache

    fetches = 0

    async def loader(agent_id: str) -> dict:
        nonlocal fetches
        fetches += 1
        return {"agent_id": agent_id, "v": fetches}

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    await cache.get("a-1")
    cache.invalidate("a-1")
    await cache.get("a-1")
    assert fetches == 2


@pytest.mark.asyncio
async def test_cache_invalidate_unknown_key_is_noop():
    from surogates.runtime import RuntimeConfigCache

    async def loader(_):
        return {}

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    cache.invalidate("does-not-exist")  # should not raise


@pytest.mark.asyncio
async def test_cache_invalidate_all_drops_every_entry():
    from surogates.runtime import RuntimeConfigCache

    fetches = 0

    async def loader(agent_id):
        nonlocal fetches
        fetches += 1
        return {"agent_id": agent_id}

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    await cache.get("a-1")
    await cache.get("a-2")
    cache.invalidate_all()
    await cache.get("a-1")
    await cache.get("a-2")
    assert fetches == 4


@pytest.mark.asyncio
async def test_cache_dedupes_concurrent_misses_for_same_key():
    """Two concurrent ``get('a-1')`` calls with empty cache must result
    in exactly one loader invocation — the second waits on the first.
    """
    from surogates.runtime import RuntimeConfigCache

    fetches = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def loader(agent_id: str) -> dict:
        nonlocal fetches
        fetches += 1
        started.set()
        await release.wait()
        return {"agent_id": agent_id, "v": fetches}

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    task1 = asyncio.create_task(cache.get("a-1"))
    await started.wait()
    task2 = asyncio.create_task(cache.get("a-1"))
    await asyncio.sleep(0)  # let task2 hit the per-key lock
    release.set()
    a, b = await asyncio.gather(task1, task2)
    assert a is b
    assert fetches == 1


@pytest.mark.asyncio
async def test_cache_does_not_dedupe_misses_for_different_keys():
    """Two concurrent gets for *different* keys must both fire their
    own loader invocation in parallel — the per-key lock must not be
    a global bottleneck."""
    from surogates.runtime import RuntimeConfigCache

    fetches = 0
    seen: list[str] = []

    async def loader(agent_id: str) -> dict:
        nonlocal fetches
        fetches += 1
        seen.append(agent_id)
        # Yield so both tasks observably enter together.
        await asyncio.sleep(0)
        return {"agent_id": agent_id}

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    await asyncio.gather(cache.get("a-1"), cache.get("a-2"))
    assert fetches == 2
    assert set(seen) == {"a-1", "a-2"}


@pytest.mark.asyncio
async def test_cache_propagates_loader_exceptions_and_does_not_cache_them():
    """When the loader raises, the next call retries — failures are not
    memoised.  Critical for the LookupError path: a 404 followed by an
    agent being promoted to runtime_kind=shared must light up the cache
    on the next call.
    """
    from surogates.runtime import RuntimeConfigCache

    attempts = 0

    async def loader(_: str) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LookupError("not yet")
        return {"agent_id": "a-1"}

    cache = RuntimeConfigCache(loader=loader, ttl_seconds=10)
    with pytest.raises(LookupError):
        await cache.get("a-1")
    cfg = await cache.get("a-1")
    assert cfg["agent_id"] == "a-1"
    assert attempts == 2
