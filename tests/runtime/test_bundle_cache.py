"""Tests for FileBundleCache L1.

Plan 3 / Task 6.  Same TTL + per-key-lock + double-checked-locking
shape as RuntimeConfigCache (Plan 1), FirebaseConfigCache (Plan 1b),
SlugResolverCache (Plan 1b).  Positive-only memoisation — a
LookupError (file missing) must not be cached because the bundle
contents the version pointer mutates inside a session is
exceedingly rare but happens via admin rollback; a negatively-
cached miss would block the rollback for a TTL window.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_bundle_cache_hits_within_ttl():
    from surogates.runtime import FileBundleCache

    fetches = 0

    async def loader(agent_id):
        nonlocal fetches
        fetches += 1
        return _make_fake_bundle(agent_id)

    cache = FileBundleCache(loader=loader, ttl_seconds=10)
    assert await cache.get("a-1") is not None
    assert await cache.get("a-1") is not None
    assert fetches == 1


@pytest.mark.asyncio
async def test_bundle_cache_invalidate_drops_entry():
    from surogates.runtime import FileBundleCache

    fetches = 0

    async def loader(_):
        nonlocal fetches
        fetches += 1
        return _make_fake_bundle("a-1")

    cache = FileBundleCache(loader=loader, ttl_seconds=10)
    await cache.get("a-1")
    cache.invalidate("a-1")
    await cache.get("a-1")
    assert fetches == 2


@pytest.mark.asyncio
async def test_bundle_cache_concurrent_calls_dedup_single_loader_call():
    """Per-key lock prevents thundering-herd against the Hub.

    20 concurrent gets for the same fresh agent_id must produce
    1 loader call, not 20."""
    from surogates.runtime import FileBundleCache

    fetches = 0
    gate = asyncio.Event()

    async def loader(agent_id):
        nonlocal fetches
        fetches += 1
        await gate.wait()
        return _make_fake_bundle(agent_id)

    cache = FileBundleCache(loader=loader, ttl_seconds=10)
    tasks = [asyncio.create_task(cache.get("a-1")) for _ in range(20)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r is not None for r in results)
    assert fetches == 1


@pytest.mark.asyncio
async def test_bundle_cache_loader_exception_not_memoised():
    """Loader exceptions represent unknown state; the next call must
    retry.  Negatively-cached misses would block bundle rollback
    (admin pushes the old version back; new sessions must see it
    immediately) for a TTL window."""
    from surogates.runtime import FileBundleCache

    calls = 0

    async def loader(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LookupError("bundle missing")
        return _make_fake_bundle("a-1")

    cache = FileBundleCache(loader=loader, ttl_seconds=10)
    with pytest.raises(LookupError):
        await cache.get("a-1")
    assert (await cache.get("a-1")) is not None
    assert calls == 2


def test_bundle_cache_channel_registered_in_invalidation_channels():
    """The agent.bundle_changed: channel is pre-routed by Plan 1b
    Task 7; Plan 3 retargets it to file_bundle_cache (Task 8)."""
    from surogates.runtime import INVALIDATION_CHANNELS

    assert "agent.bundle_changed:" in INVALIDATION_CHANNELS


def _make_fake_bundle(agent_id):
    from surogates.runtime import AgentFileBundle

    return AgentFileBundle(
        agent_id=agent_id, hub_ref="acme/agents", version="v1",
        client=object(),  # not exercised in L1 tests
    )
