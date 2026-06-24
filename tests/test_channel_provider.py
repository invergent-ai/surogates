import pytest

from surogates.memory.channel_store import ChannelMemoryStore
from surogates.memory.channel_provider import ChannelMemoryProvider


class _DictBackend:
    def __init__(self):
        self.objects = {}


@pytest.fixture
def provider(monkeypatch):
    async def fake_read(be, *, bucket, key):
        return be.objects.get((bucket, key))

    async def fake_write(be, *, bucket, key, content, expected_version):
        _, ver = be.objects.get((bucket, key), ("", 0))
        be.objects[(bucket, key)] = (content, ver + 1)
        return ver + 1

    monkeypatch.setattr("surogates.memory.channel_store.read_user_memory", fake_read)
    monkeypatch.setattr("surogates.memory.channel_store.write_user_memory", fake_write)
    store = ChannelMemoryStore(backend=_DictBackend(), bucket="b", key="k")
    return ChannelMemoryProvider(store, channel_id="C123")


def test_name_is_channel(provider):
    assert provider.name == "channel"


def test_context_only_no_tools(provider):
    assert provider.get_tool_schemas() == []


def test_is_available(provider):
    assert provider.is_available() is True


def test_ingest_then_prefetch_returns_observation(provider):
    provider.ingest("deploy is broken", meta={"user_name": "Ada", "ts": "1.0"})
    recalled = provider.prefetch("deploy")
    assert "deploy is broken" in recalled
    assert "Ada" in recalled


def test_prefetch_includes_notes(provider):
    provider._store.set_notes("OPEN LOOP: who owns the rollback?")
    recalled = provider.prefetch("rollback")
    assert "who owns the rollback" in recalled


def test_prefetch_empty_when_nothing(provider):
    assert provider.prefetch("anything") == ""


def test_on_pre_compress_with_summarizer_writes_notes(provider):
    captured = {}

    def fake_summarizer(observations, existing_notes):
        captured["called"] = True
        return "DISTILLED: rollback owned by Ada"

    provider._summarizer = fake_summarizer
    provider.ingest("rollback owned by Ada", meta={"ts": "1.0"})
    out = provider.on_pre_compress([{"role": "user", "content": "x"}])
    assert captured.get("called") is True
    assert "DISTILLED" in out
    assert "DISTILLED" in provider._store.notes()


def test_append_and_recent_signals(provider):
    provider.append_signal({"external_id": "pr-1", "title": "PR #1"})
    assert provider.recent_signals()[0]["external_id"] == "pr-1"
