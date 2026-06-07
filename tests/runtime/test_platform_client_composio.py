from __future__ import annotations

import json

import httpx
import pytest

from surogates.runtime.platform_client import PlatformClient


@pytest.mark.asyncio
async def test_mint_composio_session_posts_to_agent_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "transport": "http",
            "url": "https://mcp.composio.dev/session",
            "headers": {"x-api-key": "secret"},
        })

    client = PlatformClient(
        base_url="http://ops.internal", token="runtime-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        config = await client.mint_composio_session("agent-1", "end-user-9")
    finally:
        await client.aclose()

    assert seen["url"] == "http://ops.internal/api/agents/agents/agent-1/composio/session"
    assert seen["auth"] == "Bearer runtime-token"
    assert seen["body"] == {"user_id": "end-user-9"}
    assert config == {
        "transport": "http",
        "url": "https://mcp.composio.dev/session",
        "headers": {"x-api-key": "secret"},
    }


@pytest.mark.asyncio
async def test_mint_composio_session_none_on_404_503():
    for code in (404, 503):
        client = PlatformClient(
            base_url="http://ops.internal", token="t",
            transport=httpx.MockTransport(lambda r, c=code: httpx.Response(c)),
        )
        try:
            assert await client.mint_composio_session("agent-1", "u") is None
        finally:
            await client.aclose()
