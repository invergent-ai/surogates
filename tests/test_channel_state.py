import pytest

from surogates.channels.channel_state import ChannelAdapterState


class FakeRedis:
    def __init__(self): self.kv = {}
    async def set(self, key, value, ex=None): self.kv[key] = value
    async def get(self, key): return self.kv.get(key)
    async def exists(self, key): return 1 if key in self.kv else 0


@pytest.fixture
def slack_adapter():
    return ChannelAdapterState(FakeRedis(), agent_id="agent-1", platform="slack")


@pytest.fixture
def telegram_adapter():
    return ChannelAdapterState(FakeRedis(), agent_id="agent-1", platform="telegram")


@pytest.mark.asyncio
async def test_session_roundtrip(slack_adapter):
    assert await slack_adapter.get_session("k") is None
    await slack_adapter.remember_session("k", "sess-123")
    assert await slack_adapter.get_session("k") == "sess-123"


@pytest.mark.asyncio
async def test_get_session_decodes_bytes(slack_adapter):
    await slack_adapter.remember_session("k", "sess-9")
    slack_adapter._redis.kv[next(iter(slack_adapter._redis.kv))] = b"sess-9"
    assert await slack_adapter.get_session("k") == "sess-9"


@pytest.mark.asyncio
async def test_mentioned_thread(slack_adapter):
    assert await slack_adapter.is_mentioned_thread("t1") is False
    await slack_adapter.mark_mentioned_thread("t1")
    assert await slack_adapter.is_mentioned_thread("t1") is True


@pytest.mark.asyncio
async def test_bot_message(slack_adapter):
    assert await slack_adapter.is_bot_message("1.0") is False
    await slack_adapter.mark_bot_message("1.0")
    assert await slack_adapter.is_bot_message("1.0") is True


@pytest.mark.asyncio
async def test_keys_are_agent_scoped(slack_adapter):
    await slack_adapter.mark_mentioned_thread("t1")
    other = ChannelAdapterState(slack_adapter._redis, agent_id="agent-2", platform="slack")
    assert await other.is_mentioned_thread("t1") is False


@pytest.mark.asyncio
async def test_keys_include_platform_slack(slack_adapter):
    """Keys for slack platform include 'slack' in them."""
    await slack_adapter.mark_mentioned_thread("t1")
    keys = list(slack_adapter._redis.kv.keys())
    assert len(keys) == 1
    assert "slack" in keys[0]
    assert "telegram" not in keys[0]


@pytest.mark.asyncio
async def test_keys_include_platform_telegram(telegram_adapter):
    """Keys for telegram platform include 'telegram' in them."""
    await telegram_adapter.mark_mentioned_thread("t1")
    keys = list(telegram_adapter._redis.kv.keys())
    assert len(keys) == 1
    assert "telegram" in keys[0]
    assert "slack" not in keys[0]


@pytest.mark.asyncio
async def test_slack_and_telegram_dont_share_keys():
    """Same agent_id on different platforms must not collide in key-space."""
    redis = FakeRedis()
    slack = ChannelAdapterState(redis, agent_id="agent-1", platform="slack")
    telegram = ChannelAdapterState(redis, agent_id="agent-1", platform="telegram")

    await slack.mark_mentioned_thread("t1")
    # Telegram should NOT see the slack thread as mentioned
    assert await telegram.is_mentioned_thread("t1") is False

    await telegram.mark_bot_message("m1")
    # Slack should NOT see the telegram bot message
    assert await slack.is_bot_message("m1") is False
