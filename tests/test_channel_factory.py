import pytest
from types import SimpleNamespace

from surogates.memory.channel_factory import build_channel_provider


class _Redis:
    def __init__(self):
        self.items = []

    async def lpop(self, key, count=None):
        if not self.items:
            return None
        out = self.items[:count]
        self.items = self.items[count:]
        return out


@pytest.fixture(autouse=True)
def _fake_io(monkeypatch):
    async def fake_read(be, *, bucket, key): return None
    async def fake_write(be, *, bucket, key, content, expected_version): return 1
    monkeypatch.setattr("surogates.memory.channel_store.read_user_memory", fake_read)
    monkeypatch.setattr("surogates.memory.channel_store.write_user_memory", fake_write)


def _session(**kw):
    base = dict(
        channel="slack",
        config={"mate_follow": True, "slack_channel_id": "C123"},
        org_id="org-1", agent_id="agent-1",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_returns_provider_for_follow_channel():
    p = await build_channel_provider(
        _session(), storage_backend=object(), bucket="b", redis_client=_Redis(),
    )
    assert p is not None
    assert p.name == "channel"


@pytest.mark.asyncio
async def test_returns_none_when_follow_disabled():
    p = await build_channel_provider(
        _session(config={"slack_channel_id": "C123"}),
        storage_backend=object(), bucket="b", redis_client=_Redis(),
    )
    assert p is None


@pytest.mark.asyncio
async def test_returns_none_for_web_session():
    p = await build_channel_provider(
        _session(channel="web"), storage_backend=object(), bucket="b", redis_client=_Redis(),
    )
    assert p is None


@pytest.mark.asyncio
async def test_drains_queued_observations_before_prefetch():
    redis = _Redis()
    redis.items = [
        b'{"content": "ci red", "source": {"user_name": "Ada"}, "ts": "1.0"}',
    ]
    p = await build_channel_provider(
        _session(), storage_backend=object(), bucket="b", redis_client=redis,
    )
    assert p is not None
    assert "ci red" in p.prefetch("ci")
    assert redis.items == []


@pytest.mark.asyncio
async def test_follow_enabled_override_true_builds_even_without_config_flag():
    s = _session(config={"slack_channel_id": "C123"})  # no mate_follow flag
    p = await build_channel_provider(
        s, storage_backend=object(), bucket="b", redis_client=_Redis(),
        follow_enabled=True,
    )
    assert p is not None


@pytest.mark.asyncio
async def test_follow_enabled_override_false_blocks_even_with_config_flag():
    s = _session()  # config has mate_follow=True
    p = await build_channel_provider(
        s, storage_backend=object(), bucket="b", redis_client=_Redis(),
        follow_enabled=False,
    )
    assert p is None
