import pytest

from surogates.runtime.mate_settings_cache import MateSettingsCache, mate_cache_key


def test_cache_key():
    assert mate_cache_key("a1", "slack", "C1") == "a1:slack:C1"


@pytest.mark.asyncio
async def test_get_calls_loader_and_caches():
    calls = []

    async def loader(key):
        calls.append(key)
        return {"follow_enabled": True}

    cache = MateSettingsCache(loader=loader, ttl_seconds=60.0)
    assert await cache.get("a1:slack:C1") == {"follow_enabled": True}
    assert await cache.get("a1:slack:C1") == {"follow_enabled": True}
    assert calls == ["a1:slack:C1"]  # second hit served from cache


@pytest.mark.asyncio
async def test_invalidate_forces_reload():
    calls = []

    async def loader(key):
        calls.append(key)
        return {"n": len(calls)}

    cache = MateSettingsCache(loader=loader, ttl_seconds=60.0)
    await cache.get("k")
    cache.invalidate("k")
    await cache.get("k")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_wildcard_invalidate_drops_agent_platform_channels():
    calls = []

    async def loader(key):
        calls.append(key)
        return {"key": key}

    cache = MateSettingsCache(loader=loader, ttl_seconds=60.0)
    await cache.get("a1:slack:C1")
    await cache.get("a1:slack:C2")
    await cache.get("a1:telegram:T1")
    cache.invalidate("a1:slack:*")
    await cache.get("a1:slack:C1")
    await cache.get("a1:slack:C2")
    await cache.get("a1:telegram:T1")
    assert calls == [
        "a1:slack:C1", "a1:slack:C2", "a1:telegram:T1",
        "a1:slack:C1", "a1:slack:C2",
    ]
