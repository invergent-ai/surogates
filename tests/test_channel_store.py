import json

import pytest

from surogates.memory.channel_store import ChannelMemoryStore


class FakeBackend:
    """In-memory stand-in for the R2 backend used by memory_io."""
    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[str, int]] = {}


@pytest.fixture
def store(monkeypatch):
    backend = FakeBackend()

    async def fake_read(be, *, bucket, key):
        return be.objects.get((bucket, key))

    async def fake_write(be, *, bucket, key, content, expected_version):
        _, ver = be.objects.get((bucket, key), ("", 0))
        new_ver = ver + 1
        be.objects[(bucket, key)] = (content, new_ver)
        return new_ver

    monkeypatch.setattr("surogates.memory.channel_store.read_user_memory", fake_read)
    monkeypatch.setattr("surogates.memory.channel_store.write_user_memory", fake_write)
    return ChannelMemoryStore(backend=backend, bucket="b", key="channel/agent/C123")


@pytest.mark.asyncio
async def test_load_empty_is_blank(store):
    await store.load()
    assert store.notes() == ""
    assert store.recent_observations() == []
    assert store.signals() == []


@pytest.mark.asyncio
async def test_append_and_recent(store):
    await store.load()
    store.append_observation("hello", meta={"user": "U1", "ts": "1.0"})
    store.append_observation("world", meta={"user": "U2", "ts": "2.0"})
    recent = store.recent_observations()
    assert [o["text"] for o in recent] == ["hello", "world"]
    assert recent[0]["meta"]["user"] == "U1"


@pytest.mark.asyncio
async def test_observations_are_bounded_fifo(store):
    await store.load()
    s = ChannelMemoryStore(backend=store._backend, bucket="b", key="k2", max_observations=3)
    for i in range(5):
        s.append_observation(f"m{i}", meta={"ts": str(i)})
    texts = [o["text"] for o in s.recent_observations()]
    assert texts == ["m2", "m3", "m4"]


@pytest.mark.asyncio
async def test_notes_signals_roundtrip_through_r2(store):
    await store.load()
    store.set_notes("decisions: ship on friday")
    store.append_signal({"external_id": "pr-1", "title": "PR #1"})
    await store.flush()

    reloaded = ChannelMemoryStore(backend=store._backend, bucket="b", key="channel/agent/C123")
    # reuse the same monkeypatched memory_io via the same module
    await reloaded.load()
    assert reloaded.notes() == "decisions: ship on friday"
    assert reloaded.signals()[0]["external_id"] == "pr-1"
