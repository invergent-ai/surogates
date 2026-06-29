from surogates.channels.channel_progress import (
    progress_key, set_placeholder, read_placeholder, clear_placeholder,
    take_progress_update,
)


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.ttl = {}
    async def set(self, k, v, ex=None):
        self.kv[k] = v
        self.ttl[k] = ex
    async def get(self, k): return self.kv.get(k)
    async def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None)
            self.ttl.pop(k, None)


def test_progress_key_shape():
    assert progress_key("slack", "s1") == "channel-progress:slack:s1"


async def test_set_read_roundtrip():
    r = FakeRedis()
    await set_placeholder(r, "slack", "s1", channel="C1", ts="111.1", thread_ts="100.0")
    assert await read_placeholder(r, "slack", "s1") == {
        "channel": "C1", "ts": "111.1", "thread_ts": "100.0"}
    assert r.ttl[progress_key("slack", "s1")] == 600


async def test_read_miss_returns_none():
    assert await read_placeholder(FakeRedis(), "slack", "nope") is None


async def test_clear_removes_key():
    r = FakeRedis()
    await set_placeholder(r, "slack", "s1", channel="C1", ts="1", thread_ts=None)
    await clear_placeholder(r, "slack", "s1")
    assert await read_placeholder(r, "slack", "s1") is None


async def test_take_returns_ts_on_channel_match_without_clearing():
    r = FakeRedis()
    await set_placeholder(r, "slack", "s1", channel="C1", ts="111.1", thread_ts=None)
    out = await take_progress_update(r, "slack", "s1", "C1")
    assert out == "111.1"
    assert await read_placeholder(r, "slack", "s1") is not None  # NOT cleared on match


async def test_take_clears_and_returns_none_on_channel_mismatch():
    r = FakeRedis()
    await set_placeholder(r, "slack", "s1", channel="C1", ts="111.1", thread_ts=None)
    out = await take_progress_update(r, "slack", "s1", "C_OTHER")
    assert out is None
    assert await read_placeholder(r, "slack", "s1") is None  # stale → cleared


async def test_take_returns_none_when_absent():
    assert await take_progress_update(FakeRedis(), "slack", "s1", "C1") is None
