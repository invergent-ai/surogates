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


# ---------------------------------------------------------------------------
# L2 disk cache + read-through wrapper (Plan 3 / Task 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_cache_l2_writes_file_to_disk(tmp_path):
    """L2 disk cache: when the L1 loader fetches a file the bytes
    are written atomically to disk so a worker restart doesn't
    blast the Hub re-pulling everything."""
    from surogates.runtime.bundle_cache import _L2DiskCache

    cache = _L2DiskCache(root=tmp_path, max_bytes=10_000)
    await cache.write("a-1", "v1", "SOUL.md", b"hello")
    assert (tmp_path / "a-1" / "v1" / "SOUL.md").read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_bundle_cache_l2_read_hit(tmp_path):
    from surogates.runtime.bundle_cache import _L2DiskCache

    cache = _L2DiskCache(root=tmp_path, max_bytes=10_000)
    await cache.write("a-1", "v1", "SOUL.md", b"hello")
    assert await cache.read("a-1", "v1", "SOUL.md") == b"hello"


@pytest.mark.asyncio
async def test_bundle_cache_l2_read_miss_returns_none(tmp_path):
    from surogates.runtime.bundle_cache import _L2DiskCache

    cache = _L2DiskCache(root=tmp_path, max_bytes=10_000)
    assert await cache.read("a-1", "v1", "missing.md") is None


@pytest.mark.asyncio
async def test_bundle_cache_l2_atomic_write(tmp_path):
    """A concurrent reader must never see a partially-written file.
    The cache writes to a .tmp sibling and renames into place."""
    from surogates.runtime.bundle_cache import _L2DiskCache

    cache = _L2DiskCache(root=tmp_path, max_bytes=10_000)
    await cache.write("a-1", "v1", "SOUL.md", b"complete")
    final = tmp_path / "a-1" / "v1" / "SOUL.md"
    assert not list(final.parent.glob("*.tmp"))


@pytest.mark.asyncio
async def test_bundle_cache_l2_invalidate_agent_drops_all_versions(tmp_path):
    """When ``agent.bundle_changed:<agent_id>`` fires the cache
    drops every cached version for that agent_id (not just the
    one matching the new pointer) — operators rolling back can
    pick any prior version and the cache must re-fetch."""
    from surogates.runtime.bundle_cache import _L2DiskCache

    cache = _L2DiskCache(root=tmp_path, max_bytes=10_000)
    await cache.write("a-1", "v1", "SOUL.md", b"v1-content")
    await cache.write("a-1", "v2", "SOUL.md", b"v2-content")
    await cache.invalidate_agent("a-1")
    assert await cache.read("a-1", "v1", "SOUL.md") is None
    assert await cache.read("a-1", "v2", "SOUL.md") is None


@pytest.mark.asyncio
async def test_l2_read_through_hub_hits_l2_on_repeat_read(tmp_path):
    """The L2 read-through wrapper checks disk first; on warm read
    the Hub is not touched at all."""
    from surogates.runtime.bundle_cache import (
        _L2DiskCache, _L2ReadThroughHub,
    )

    class _FakeHub:
        def __init__(self):
            self.read_calls = 0

        async def read_bytes(self, ref, path):
            self.read_calls += 1
            return b"hub-bytes"

        async def list_paths(self, ref, *, prefix=""):
            return []

        async def aclose(self):
            return None

    l2 = _L2DiskCache(root=tmp_path, max_bytes=10_000)
    hub = _FakeHub()
    wrapper = _L2ReadThroughHub(agent_id="a-1", hub=hub, l2=l2)

    # First read misses L2 → hub call + write-through.
    assert await wrapper.read_bytes("v1", "SOUL.md") == b"hub-bytes"
    # Second read hits L2 → no extra hub call.
    assert await wrapper.read_bytes("v1", "SOUL.md") == b"hub-bytes"
    assert hub.read_calls == 1


@pytest.mark.asyncio
async def test_l2_read_through_hub_forwards_list_paths(tmp_path):
    """list_paths is not L2-cached; the wrapper forwards verbatim
    so directory listings always reflect the current Hub view."""
    from surogates.runtime.bundle_cache import (
        _L2DiskCache, _L2ReadThroughHub,
    )

    class _FakeHub:
        async def read_bytes(self, ref, path):
            raise AssertionError("not called")

        async def list_paths(self, ref, *, prefix=""):
            return ["SOUL.md", "skills/foo/SKILL.md"]

        async def aclose(self):
            return None

    l2 = _L2DiskCache(root=tmp_path, max_bytes=10_000)
    wrapper = _L2ReadThroughHub(
        agent_id="a-1", hub=_FakeHub(), l2=l2,
    )
    assert await wrapper.list_paths("v1", prefix="skills/") == [
        "SOUL.md", "skills/foo/SKILL.md",
    ]
