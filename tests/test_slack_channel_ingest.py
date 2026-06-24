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


def test_follow_gate_reads_env_allowlist(monkeypatch):
    adapter = SlackAdapter.__new__(SlackAdapter)
    monkeypatch.setenv("SUROGATES_MATE_FOLLOW_CHANNELS", "C1, C2")
    assert adapter._follow_enabled_channel("C1") is True
    assert adapter._follow_enabled_channel("C2") is True
    assert adapter._follow_enabled_channel("C9") is False


def test_follow_gate_disabled_when_unset(monkeypatch):
    adapter = SlackAdapter.__new__(SlackAdapter)
    monkeypatch.delenv("SUROGATES_MATE_FOLLOW_CHANNELS", raising=False)
    assert adapter._follow_enabled_channel("C1") is False
