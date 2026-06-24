import pytest

from surogates.channels.slack import SlackAdapter
from surogates.channels.slack_state import SlackAdapterState


class FakeRedis:
    def __init__(self): self.kv = {}
    async def set(self, key, value, ex=None): self.kv[key] = value
    async def get(self, key): return self.kv.get(key)
    async def exists(self, key): return 1 if key in self.kv else 0


@pytest.mark.asyncio
async def test_mentioned_thread_seen_after_mark():
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._state = SlackAdapterState(FakeRedis(), agent_id="agent-1")
    await adapter._state.mark_mentioned_thread("t1")
    assert await adapter._state.is_mentioned_thread("t1") is True


@pytest.mark.asyncio
async def test_has_active_session_for_thread_redis_backed():
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._state = SlackAdapterState(FakeRedis(), agent_id="agent-1")
    assert await SlackAdapter._has_active_session_for_thread(
        adapter, "C1", "t1", "U1",
    ) is False
