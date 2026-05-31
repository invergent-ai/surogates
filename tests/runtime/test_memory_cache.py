"""Tests for MemoryCache L1.

Same TTL + per-key-lock + double-checked-locking
shape as RuntimeConfigCache / FirebaseConfigCache /
SlugResolverCache / FileBundleCache.  Key is ``"<org_id>:<user_id>"``
verbatim so the invalidator can pass the channel
identifier through without a parser.

Loader exceptions are NOT memoised — a transient R2 failure on
call N must let call N+1 retry instead of poisoning the cache.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_memory_cache_hits_within_ttl():
    from surogates.runtime import MemoryCache

    fetches = 0

    async def loader(_key):
        nonlocal fetches
        fetches += 1
        return b"hello"

    cache = MemoryCache(loader=loader, ttl_seconds=10)
    assert await cache.get("o-1:u-1") == b"hello"
    assert await cache.get("o-1:u-1") == b"hello"
    assert fetches == 1


@pytest.mark.asyncio
async def test_memory_cache_invalidate_drops_entry():
    from surogates.runtime import MemoryCache

    fetches = 0

    async def loader(_):
        nonlocal fetches
        fetches += 1
        return b"v"

    cache = MemoryCache(loader=loader, ttl_seconds=10)
    await cache.get("o-1:u-1")
    cache.invalidate("o-1:u-1")
    await cache.get("o-1:u-1")
    assert fetches == 2


@pytest.mark.asyncio
async def test_memory_cache_concurrent_dedup():
    """Per-key lock prevents thundering-herd against R2.  20
    concurrent gets for the same fresh (org_id, user_id) tuple
    produce 1 loader call, not 20."""
    from surogates.runtime import MemoryCache

    fetches = 0
    gate = asyncio.Event()

    async def loader(_):
        nonlocal fetches
        fetches += 1
        await gate.wait()
        return b"v"

    cache = MemoryCache(loader=loader, ttl_seconds=10)
    tasks = [asyncio.create_task(cache.get("o-1:u-1")) for _ in range(20)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r == b"v" for r in results)
    assert fetches == 1


@pytest.mark.asyncio
async def test_memory_cache_loader_exception_not_memoised():
    """A transient R2 error must not poison the cache for a TTL
    window — the next read retries."""
    from surogates.runtime import MemoryCache

    calls = 0

    async def loader(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("R2 timeout")
        return b"v"

    cache = MemoryCache(loader=loader, ttl_seconds=10)
    with pytest.raises(RuntimeError):
        await cache.get("o-1:u-1")
    assert await cache.get("o-1:u-1") == b"v"
    assert calls == 2


@pytest.mark.asyncio
async def test_memory_cache_isolates_keys():
    """Two different (org_id, user_id) tuples get independent
    entries — one user's invalidation doesn't drop another's."""
    from surogates.runtime import MemoryCache

    counter = 0

    async def loader(key):
        nonlocal counter
        counter += 1
        return f"loader-call-{counter}-for-{key}".encode()

    cache = MemoryCache(loader=loader, ttl_seconds=10)
    a = await cache.get("o-1:u-A")
    b = await cache.get("o-1:u-B")
    assert a != b
    cache.invalidate("o-1:u-A")
    a2 = await cache.get("o-1:u-A")
    b_again = await cache.get("o-1:u-B")
    # A was reloaded; B was served from cache (same bytes as the
    # first B fetch).
    assert a2 != a
    assert b_again == b
