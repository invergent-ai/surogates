import json

from surogates.harness.api_client import HarnessAPIClient


def _client(session_id="11111111-1111-1111-1111-111111111111"):
    return HarnessAPIClient(base_url="http://api", token="t", session_id=session_id)


async def test_requires_session_id():
    c = HarnessAPIClient(base_url="http://api", token="t", session_id=None)
    out = json.loads(await c.fetch_channel_messages(limit=10))
    assert out["success"] is False


async def test_posts_to_channel_messages_path(monkeypatch):
    c = _client()
    captured = {}

    async def _fake_post(path, body=None):
        captured["path"] = path
        captured["body"] = body
        return {"count": 2, "messages_block": "…", "channel": "surogate", "note": None}

    monkeypatch.setattr(c, "_post", _fake_post)
    out = json.loads(await c.fetch_channel_messages(limit=10, since="7d", user="<@U1>"))
    assert out["success"] is True
    assert out["count"] == 2
    assert captured["path"] == (
        "/v1/sessions/11111111-1111-1111-1111-111111111111/channel-messages")
    assert captured["body"] == {"limit": 10, "since": "7d", "user": "<@U1>"}
