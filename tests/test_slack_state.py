import pytest

from surogates.channels.slack_state import SlackAdapterState


class FakeRedis:
    def __init__(self): self.kv = {}
    async def set(self, key, value, ex=None): self.kv[key] = value
    async def get(self, key): return self.kv.get(key)
    async def exists(self, key): return 1 if key in self.kv else 0


@pytest.fixture
def state():
    return SlackAdapterState(FakeRedis(), agent_id="agent-1")


@pytest.mark.asyncio
async def test_session_roundtrip(state):
    assert await state.get_session("k") is None
    await state.remember_session("k", "sess-123")
    assert await state.get_session("k") == "sess-123"


@pytest.mark.asyncio
async def test_get_session_decodes_bytes(state):
    await state.remember_session("k", "sess-9")
    state._redis.kv[next(iter(state._redis.kv))] = b"sess-9"
    assert await state.get_session("k") == "sess-9"


@pytest.mark.asyncio
async def test_mentioned_thread(state):
    assert await state.is_mentioned_thread("t1") is False
    await state.mark_mentioned_thread("t1")
    assert await state.is_mentioned_thread("t1") is True


@pytest.mark.asyncio
async def test_bot_message(state):
    assert await state.is_bot_message("1.0") is False
    await state.mark_bot_message("1.0")
    assert await state.is_bot_message("1.0") is True


@pytest.mark.asyncio
async def test_keys_are_agent_scoped(state):
    await state.mark_mentioned_thread("t1")
    other = SlackAdapterState(state._redis, agent_id="agent-2")
    assert await other.is_mentioned_thread("t1") is False
