import json
import pytest
from surogates.channels.channel_backfill import (
    cache_key, read_block, write_block, in_negative_cooldown, mark_negative, is_stale,
)

class FakeRedis:
    def __init__(self): self.kv = {}
    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    async def get(self, k): return self.kv.get(k)

def test_cache_key_shape():
    k = cache_key(org_id="o1", agent_id="a1", kind="slack", identifier="A0X", channel_id="C9")
    assert k == "channel-backfill:o1:a1:slack:A0X:C9"

@pytest.mark.asyncio
async def test_write_then_read_roundtrips_block_and_time():
    r = FakeRedis()
    k = cache_key(org_id="o", agent_id="a", kind="slack", identifier="A", channel_id="C")
    await write_block(r, k, "BLOCK", fetched_at=123.0, ttl_s=3600)
    got = await read_block(r, k)
    assert got == ("BLOCK", 123.0)

@pytest.mark.asyncio
async def test_read_miss_returns_none():
    assert await read_block(FakeRedis(), "nope") is None

@pytest.mark.asyncio
async def test_negative_cooldown_roundtrip():
    r = FakeRedis()
    k = cache_key(org_id="o", agent_id="a", kind="slack", identifier="A", channel_id="C")
    assert await in_negative_cooldown(r, k) is False
    await mark_negative(r, k, cooldown_s=600)
    assert await in_negative_cooldown(r, k) is True

def test_is_stale():
    assert is_stale(100.0, now=100.0 + 7200, ttl_s=3600) is True
    assert is_stale(100.0, now=100.0 + 60, ttl_s=3600) is False
