import pytest

from surogates.ambient.rate_limiter import AmbientRateLimiter


class FakeRedis:
    def __init__(self): self.kv = {}
    async def incr(self, k): self.kv[k] = int(self.kv.get(k, 0)) + 1; return self.kv[k]
    async def expire(self, k, s): pass
    async def get(self, k): return self.kv.get(k)
    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True
    async def ttl(self, k): return -1


@pytest.fixture
def limiter():
    return AmbientRateLimiter(FakeRedis())


@pytest.mark.asyncio
async def test_allow_post_under_daily_cap(limiter):
    assert await limiter.allow_post(
        agent_id="a", channel_id="C1", max_per_day=2, min_seconds_between=0,
    ) is True


@pytest.mark.asyncio
async def test_daily_cap_blocks_after_max(limiter):
    for _ in range(2):
        await limiter.record_post(agent_id="a", channel_id="C1")
    assert await limiter.allow_post(
        agent_id="a", channel_id="C1", max_per_day=2, min_seconds_between=0,
    ) is False


@pytest.mark.asyncio
async def test_min_gap_blocks_when_recent(limiter):
    await limiter.record_post_gap(
        agent_id="a", channel_id="C1", min_seconds_between=600,
    )
    assert await limiter.allow_post(
        agent_id="a", channel_id="C1", max_per_day=99, min_seconds_between=600,
    ) is False


@pytest.mark.asyncio
async def test_revive_once_per_window(limiter):
    assert await limiter.allow_revive(agent_id="a", thread_ts="t1", window_seconds=3600) is True
    assert await limiter.allow_revive(agent_id="a", thread_ts="t1", window_seconds=3600) is False
