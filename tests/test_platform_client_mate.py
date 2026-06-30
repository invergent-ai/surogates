import httpx
import pytest

from surogates.runtime.platform_client import PlatformClient


def _client(handler):
    transport = httpx.MockTransport(handler)
    return PlatformClient(base_url="http://ops", token="t", transport=transport)


@pytest.mark.asyncio
async def test_returns_settings_dict():
    def handler(request):
        assert request.url.path == "/api/mate/runtime/a1/slack/C1"
        return httpx.Response(200, json={"follow_enabled": True})
    pc = _client(handler)
    out = await pc.get_mate_channel_settings("a1", "slack", "C1")
    assert out == {"follow_enabled": True}


@pytest.mark.asyncio
async def test_404_is_none():
    def handler(request):
        return httpx.Response(404, json={"detail": "x"})
    pc = _client(handler)
    assert await pc.get_mate_channel_settings("a1", "slack", "C1") is None
