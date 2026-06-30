from surogates.channels.channel_progress import (
    progress_key, set_placeholder, read_placeholder, clear_placeholder,
    take_progress_update, post_placeholder_once,
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


async def test_post_once_posts_and_stores_when_absent():
    r = FakeRedis()
    calls = []
    async def _post():
        calls.append(1)
        return "111.1"
    ts = await post_placeholder_once(
        r, "slack", "s1", post=_post, channel="C1", thread_ts="100.0")
    assert ts == "111.1"
    assert calls == [1]  # posted once
    assert await read_placeholder(r, "slack", "s1") == {
        "channel": "C1", "ts": "111.1", "thread_ts": "100.0"}


async def test_post_once_skips_when_placeholder_already_pending():
    r = FakeRedis()
    await set_placeholder(r, "slack", "s1", channel="C1", ts="999.9", thread_ts=None)
    calls = []
    async def _post():
        calls.append(1)
        return "222.2"
    ts = await post_placeholder_once(
        r, "slack", "s1", post=_post, channel="C1", thread_ts="100.0")
    assert ts is None          # did not post a second placeholder
    assert calls == []         # post callable never invoked
    # the existing placeholder is left untouched (it will be edited into the reply)
    assert (await read_placeholder(r, "slack", "s1"))["ts"] == "999.9"


async def test_post_once_does_not_store_when_post_returns_none():
    r = FakeRedis()
    async def _post():
        return None
    ts = await post_placeholder_once(
        r, "slack", "s1", post=_post, channel="C1", thread_ts=None)
    assert ts is None
    assert await read_placeholder(r, "slack", "s1") is None
