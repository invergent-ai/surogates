"""Tests for MCPServerRegistryCache.

Per-(org_id, agent_id) cache of the MCP server
catalog the agent can call.  Same TTL + per-key-lock + double-
checked-locking shape as RuntimeConfigCache /
FirebaseConfigCache / SlugResolverCache / FileBundleCache /
MemoryCache.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_mcp_server_cache_hits_within_ttl():
    from surogates.runtime import MCPServerRegistryCache

    fetches = 0

    async def loader(_):
        nonlocal fetches
        fetches += 1
        return [{"name": "fs", "transport": "stdio"}]

    # Key shape is the bare agent_id -- see mcp_server_cache.py
    # docstring for why it diverges from MemoryCache's colon shape.
    cache = MCPServerRegistryCache(loader=loader, ttl_seconds=10)
    assert (await cache.get("a-1"))[0]["name"] == "fs"
    assert (await cache.get("a-1"))[0]["name"] == "fs"
    assert fetches == 1


@pytest.mark.asyncio
async def test_mcp_server_cache_invalidate_drops_entry():
    from surogates.runtime import MCPServerRegistryCache

    fetches = 0

    async def loader(_):
        nonlocal fetches
        fetches += 1
        return []

    cache = MCPServerRegistryCache(loader=loader, ttl_seconds=10)
    await cache.get("a-1")
    cache.invalidate("a-1")
    await cache.get("a-1")
    assert fetches == 2


@pytest.mark.asyncio
async def test_mcp_server_cache_concurrent_dedup():
    from surogates.runtime import MCPServerRegistryCache

    fetches = 0
    gate = asyncio.Event()

    async def loader(_):
        nonlocal fetches
        fetches += 1
        await gate.wait()
        return []

    cache = MCPServerRegistryCache(loader=loader, ttl_seconds=10)
    tasks = [asyncio.create_task(cache.get("a-1")) for _ in range(20)]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    assert fetches == 1


@pytest.mark.asyncio
async def test_mcp_server_cache_loader_exception_not_memoised():
    from surogates.runtime import MCPServerRegistryCache

    calls = 0

    async def loader(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("DB blip")
        return []

    cache = MCPServerRegistryCache(loader=loader, ttl_seconds=10)
    with pytest.raises(RuntimeError):
        await cache.get("a-1")
    await cache.get("a-1")
    assert calls == 2
