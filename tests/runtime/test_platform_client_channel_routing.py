"""PlatformClient method that powers the
ChannelRoutingCache loader.

Tests the HTTP shape (200/404/401) the cache relies on:

- 200 with the routing record -> loader returns the dict.
- 404 -> loader returns None (the ChannelRoutingCache's
  negative-memoise path covers this).
- 401 -> PlatformAuthError (operations problem; should NOT be
  memoised as a missing routing).
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_get_channel_routing_returns_dict_on_200():
    from surogates.runtime.platform_client import PlatformClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            "/api/channels/by-identifier/slack/A0123ABCD"
        )
        return httpx.Response(200, json={
            "org_id": "o-1", "agent_id": "a-1",
            "api_web_url": "https://web.acme",
        })

    client = PlatformClient(
        base_url="http://platform",
        token="tok",
        transport=httpx.MockTransport(handler),
    )
    result = await client.get_channel_routing("slack", "A0123ABCD")
    assert result == {
        "org_id": "o-1", "agent_id": "a-1",
        "api_web_url": "https://web.acme",
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_get_channel_routing_returns_none_on_404():
    """404 is a normal state -- no routing configured for this
    inbound identifier is common (every spurious Slack event for
    a workspace we don't serve).  Mirror SlugResolverCache's
    contract that returns None rather than LookupError so the
    cache's negative-memoise path is the single code path."""
    from surogates.runtime.platform_client import PlatformClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = PlatformClient(
        base_url="http://platform",
        token="tok",
        transport=httpx.MockTransport(handler),
    )
    assert await client.get_channel_routing(
        "slack", "A0123ABCD",
    ) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_get_channel_routing_raises_platform_auth_error_on_401():
    from surogates.runtime.platform_client import (
        PlatformAuthError, PlatformClient,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = PlatformClient(
        base_url="http://platform",
        token="tok",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PlatformAuthError):
        await client.get_channel_routing("slack", "A0123ABCD")
    await client.aclose()
