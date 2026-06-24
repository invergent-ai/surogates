import pytest

from surogates.channels.slack import SlackAdapter


class _Store:
    def __init__(self): self.events = []
    async def emit_event(self, session_id, etype, data):
        self.events.append((session_id, etype, data))


class _Redis:
    def __init__(self): self.pushed = []
    async def rpush(self, key, value): self.pushed.append((key, value))
    async def ltrim(self, key, start, stop): self.trim = (key, start, stop)


@pytest.mark.asyncio
async def test_ingest_appends_observation_without_session_event_or_enqueue(monkeypatch):
    store = _Store()
    redis = _Redis()
    enqueued = []

    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._session_store = store
    adapter._redis = redis
    adapter._agent_id = "agent-1"
    adapter._enqueue = lambda **kw: enqueued.append(kw)

    await SlackAdapter._ingest_channel_observation(
        adapter,
        channel_id="C1", team_id="T1",
        text="ci just went red", user_id="U9", user_name="Bo", ts="123.45",
    )

    assert store.events == []
    assert enqueued == []  # NEVER enqueue on observe
    assert redis.pushed
    key, payload = redis.pushed[0]
    assert key == "mate:channel-observations:agent-1:C1"
    assert "ci just went red" in payload
    assert '"user_id": "U9"' in payload


@pytest.mark.asyncio
async def test_ingest_skips_empty_text(monkeypatch):
    redis = _Redis()
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._redis = redis
    adapter._agent_id = "agent-1"

    await SlackAdapter._ingest_channel_observation(
        adapter,
        channel_id="C1", team_id="T1",
        text="   ", user_id="U9", user_name="Bo", ts="123.45",
    )
    assert redis.pushed == []


class _MateCache:
    def __init__(self, mapping): self._m = mapping
    async def get(self, key): return self._m.get(key)


@pytest.mark.asyncio
async def test_follow_gate_true_from_cache():
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._agent_id = "agent-1"
    adapter._mate_settings_cache = _MateCache(
        {"agent-1:slack:C1": {"follow_enabled": True}},
    )
    assert await adapter._follow_enabled_channel("C1") is True
    assert await adapter._follow_enabled_channel("C2") is False


@pytest.mark.asyncio
async def test_follow_gate_false_without_cache():
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._agent_id = "agent-1"
    adapter._mate_settings_cache = None
    assert await adapter._follow_enabled_channel("C1") is False
