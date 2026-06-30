import json

import pytest

from surogates.channels.channel_observations import (
    append_channel_observation,
    channel_observation_key,
    drain_channel_observations,
)


class FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[bytes]] = {}

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value.encode("utf-8"))

    async def ltrim(self, key, start, stop):
        items = self.lists.get(key, [])
        n = len(items)
        if start < 0:
            start = max(n + start, 0)
        if stop < 0:
            stop = n + stop
        self.lists[key] = items[start : stop + 1]

    async def lpop(self, key, count=None):
        items = self.lists.get(key, [])
        if not items:
            return None
        if count is None:
            return items.pop(0)
        out = items[:count]
        self.lists[key] = items[count:]
        return out


def test_key_is_scoped_by_agent_and_channel():
    assert channel_observation_key("agent-1", "C123") == "mate:channel-observations:agent-1:C123"


@pytest.mark.asyncio
async def test_append_and_drain_round_trip():
    redis = FakeRedis()
    await append_channel_observation(
        redis,
        agent_id="agent-1",
        channel_id="C123",
        observation={"content": "ci red", "source": {"user_name": "Ada"}},
    )

    drained = await drain_channel_observations(redis, agent_id="agent-1", channel_id="C123")
    assert drained == [{"content": "ci red", "source": {"user_name": "Ada"}}]
    assert await drain_channel_observations(redis, agent_id="agent-1", channel_id="C123") == []


@pytest.mark.asyncio
async def test_append_bounds_queue_fifo():
    redis = FakeRedis()
    for i in range(5):
        await append_channel_observation(
            redis,
            agent_id="agent-1",
            channel_id="C123",
            observation={"content": f"m{i}"},
            maxlen=3,
        )

    raw = redis.lists[channel_observation_key("agent-1", "C123")]
    assert [json.loads(item.decode("utf-8"))["content"] for item in raw] == ["m2", "m3", "m4"]
