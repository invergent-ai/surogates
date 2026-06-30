# tests/test_slack_thinking_placeholder.py
from types import SimpleNamespace
from surogates.channels.platforms.slack import SlackPlatform


class _Client:
    def __init__(self, post_ts="200.0", update_ts="100.0", update_raises=False):
        self.post_ts = post_ts
        self._update_ts = update_ts
        self.update_raises = update_raises
        self.posted = []
        self.updated = []
    async def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ts": self.post_ts}
    async def chat_update(self, **kw):
        if self.update_raises:
            raise RuntimeError("message_not_found")
        self.updated.append(kw)
        return {"ts": self._update_ts}


def _platform_with(client):
    p = SlackPlatform()
    p._get_client = lambda token: client          # type: ignore
    return p


def _item(content="hi", channel="C1", thread_ts="100.0", update_ts=None):
    dest = {"channel_id": channel, "thread_ts": thread_ts}
    if update_ts is not None:
        dest["update_ts"] = update_ts
    return SimpleNamespace(destination=dest, payload={"content": content})


async def test_post_thinking_placeholder_returns_ts():
    c = _Client(post_ts="333.3")
    ts = await _platform_with(c).post_thinking_placeholder(
        creds={"bot_token": "x"}, channel="C1", thread_ts="100.0")
    assert ts == "333.3"
    assert c.posted[0]["channel"] == "C1" and c.posted[0]["text"] == "_Thinking…_"
    assert c.posted[0]["thread_ts"] == "100.0"


async def test_post_thinking_placeholder_none_on_error():
    class Boom:
        async def chat_postMessage(self, **kw): raise RuntimeError("ratelimited")
    ts = await _platform_with(Boom()).post_thinking_placeholder(
        creds={"bot_token": "x"}, channel="C1", thread_ts=None)
    assert ts is None


async def test_send_edits_placeholder_when_update_ts_present():
    c = _Client()
    out = await _platform_with(c).send(_item(update_ts="100.0"), creds={"bot_token": "x"})
    assert out.success is True
    assert c.updated and c.updated[0]["ts"] == "100.0" and c.updated[0]["channel"] == "C1"
    assert c.posted == []  # edited, did not post a new message


async def test_send_posts_fresh_when_no_update_ts():
    c = _Client()
    out = await _platform_with(c).send(_item(), creds={"bot_token": "x"})
    assert out.success is True
    assert c.posted and c.updated == []
    assert out.message_id == "200.0"


async def test_send_falls_back_to_post_when_update_fails():
    c = _Client(update_raises=True)
    out = await _platform_with(c).send(_item(update_ts="100.0"), creds={"bot_token": "x"})
    assert out.success is True          # reply still lands
    assert c.posted and out.message_id == "200.0"   # fell back to a fresh post
