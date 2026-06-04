"""Tests for ``surogates.runtime.FirebaseConfigCache``.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_firebase_cache_hits_within_ttl():
    from surogates.runtime import FirebaseConfigCache

    fetches = 0

    async def loader(project_id: str) -> dict:
        nonlocal fetches
        fetches += 1
        return {"project_id": project_id, "v": 1}

    cache = FirebaseConfigCache(loader=loader, ttl_seconds=10)
    assert (await cache.get("p-1"))["v"] == 1
    assert (await cache.get("p-1"))["v"] == 1
    assert fetches == 1


@pytest.mark.asyncio
async def test_firebase_cache_refetches_after_ttl():
    from surogates.runtime import FirebaseConfigCache

    fetches = 0

    async def loader(project_id: str) -> dict:
        nonlocal fetches
        fetches += 1
        return {"project_id": project_id, "v": fetches}

    cache = FirebaseConfigCache(loader=loader, ttl_seconds=0.05)
    await cache.get("p-1")
    await asyncio.sleep(0.1)
    await cache.get("p-1")
    assert fetches == 2


@pytest.mark.asyncio
async def test_firebase_cache_invalidate_drops_entry():
    from surogates.runtime import FirebaseConfigCache

    fetches = 0

    async def loader(_: str) -> dict:
        nonlocal fetches
        fetches += 1
        return {}

    cache = FirebaseConfigCache(loader=loader, ttl_seconds=10)
    await cache.get("p-1")
    cache.invalidate("p-1")
    await cache.get("p-1")
    assert fetches == 2


@pytest.mark.asyncio
async def test_firebase_cache_dedupes_concurrent_misses():
    from surogates.runtime import FirebaseConfigCache

    fetches = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def loader(project_id: str) -> dict:
        nonlocal fetches
        fetches += 1
        started.set()
        await release.wait()
        return {"project_id": project_id}

    cache = FirebaseConfigCache(loader=loader, ttl_seconds=10)
    task1 = asyncio.create_task(cache.get("p-1"))
    await started.wait()
    task2 = asyncio.create_task(cache.get("p-1"))
    await asyncio.sleep(0)
    release.set()
    a, b = await asyncio.gather(task1, task2)
    assert a is b
    assert fetches == 1


@pytest.mark.asyncio
async def test_firebase_cache_does_not_memoise_loader_exceptions():
    """LookupError on first call must let the next call retry the
    loader — projects can switch on Firebase between calls and a
    cached negative would block adoption for ttl_seconds."""
    from surogates.runtime import FirebaseConfigCache

    attempts = 0

    async def loader(_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LookupError("not yet configured")
        return {"project_id": "p-1"}

    cache = FirebaseConfigCache(loader=loader, ttl_seconds=10)
    with pytest.raises(LookupError):
        await cache.get("p-1")
    cfg = await cache.get("p-1")
    assert cfg["project_id"] == "p-1"
    assert attempts == 2

