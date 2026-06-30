import json

import httpx

from surogates.harness.api_client import HarnessAPIClient


def _client(session_id="11111111-1111-1111-1111-111111111111"):
    return HarnessAPIClient(
        base_url="http://api", token="t", session_id=session_id,
    )


async def test_fetch_channel_file_posts_session_scoped_path(monkeypatch):
    seen = {}

    async def _post(path, body=None):
        seen["path"] = path
        return {"kind": "attachment", "path": "uploads/slack/fetch/F1-x",
                "filename": "x", "mime_type": "text/plain", "size": 2}

    c = _client()
    monkeypatch.setattr(c, "_post", _post)
    out = json.loads(await c.fetch_channel_file("F1"))
    assert out["success"] is True
    assert out["filename"] == "x"
    assert seen["path"] == (
        "/v1/sessions/11111111-1111-1111-1111-111111111111/channel-files/F1"
    )


async def test_fetch_channel_file_empty_id_no_request(monkeypatch):
    c = _client()

    async def _explode(*a, **k):
        raise AssertionError("must not POST for an empty file_id")

    monkeypatch.setattr(c, "_post", _explode)
    out = json.loads(await c.fetch_channel_file("  "))
    assert out["success"] is False
    assert "file_id" in out["error"].lower()


async def test_fetch_channel_file_requires_session_id():
    c = HarnessAPIClient(base_url="http://api", token="t", session_id=None)
    out = json.loads(await c.fetch_channel_file("F1"))
    assert out["success"] is False
    assert "session" in out["error"].lower()


async def test_fetch_channel_file_maps_http_error(monkeypatch):
    c = _client()

    async def _post(path, body=None):
        request = httpx.Request("POST", "http://api" + path)
        response = httpx.Response(403, json={"detail": "not shared"}, request=request)
        raise httpx.HTTPStatusError("403", request=request, response=response)

    monkeypatch.setattr(c, "_post", _post)
    out = json.loads(await c.fetch_channel_file("F1"))
    assert out["success"] is False
    assert out["error"] == "not shared"
