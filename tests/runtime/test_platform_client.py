"""Tests for ``surogates.runtime.PlatformClient``.

Thin httpx wrapper that reads per-agent runtime
configuration from the management plane's
``/api/agents/{id}/runtime-config`` endpoint.  One process-wide
instance held on ``app.state.platform_client``; consumers go through
the cache layer rather than hitting the network on every
call.
"""

from __future__ import annotations

import httpx
import pytest


def _mock_transport(handler):
    """Wrap a request → response handler into an httpx MockTransport."""
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_get_runtime_config_happy_path():
    from surogates.runtime import PlatformClient

    payload = {
        "agent_id": "a-1",
        "org_id": "o-1",
        "project_id": "p-1",
        "enabled": True,
        "version": 7,
        "api_web_url": "https://web.example.com",
        "llm_main": {"model": "gpt", "base_url": "u", "api_key_ref": "v"},
        "mcp_server_ids": ["m1"],
        "governance": {},
        "storage_key_prefix": "p-1/a-1",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/agents/agents/a-1/runtime-config"
        assert request.headers["Authorization"] == "Bearer secret-token"
        return httpx.Response(200, json=payload)

    client = PlatformClient(
        base_url="https://ops.example.com",
        token="secret-token",
        transport=_mock_transport(handler),
    )
    try:
        cfg = await client.get_runtime_config("a-1")
    finally:
        await client.aclose()

    assert cfg["agent_id"] == "a-1"
    assert cfg["version"] == 7


@pytest.mark.asyncio
async def test_get_runtime_config_404_raises_lookup_error():
    from surogates.runtime import PlatformClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not configured"})

    client = PlatformClient(
        base_url="https://ops.example.com",
        token="x",
        transport=_mock_transport(handler),
    )
    try:
        with pytest.raises(LookupError) as excinfo:
            await client.get_runtime_config("a-1")
    finally:
        await client.aclose()

    assert "a-1" in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_runtime_config_401_raises_runtime_error():
    """A 401 means our bearer token is bad — that is an operations
    problem, not a 'maybe the agent moved kinds' transient.  Surface it
    distinctly so monitoring can page rather than retry blindly.
    """
    from surogates.runtime import PlatformClient
    from surogates.runtime.platform_client import PlatformAuthError

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid"})

    client = PlatformClient(
        base_url="https://ops.example.com",
        token="bad",
        transport=_mock_transport(handler),
    )
    try:
        with pytest.raises(PlatformAuthError):
            await client.get_runtime_config("a-1")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_runtime_config_500_raises_http_error():
    """Unexpected upstream errors propagate as httpx.HTTPStatusError so
    the cache layer can fall back to a stale entry if it has one.
    """
    from surogates.runtime import PlatformClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = PlatformClient(
        base_url="https://ops.example.com",
        token="x",
        transport=_mock_transport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_runtime_config("a-1")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_agent_id_for_slug_200():
    """Happy path resolves a slug to agent_id."""
    from surogates.runtime import PlatformClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/agents/by-slug/acme"
        return httpx.Response(200, json={"agent_id": "agent-acme"})

    client = PlatformClient(
        base_url="https://ops.example.com",
        token="t",
        transport=_mock_transport(handler),
    )
    try:
        result = await client.get_agent_id_for_slug("acme")
    finally:
        await client.aclose()
    assert result == "agent-acme"


@pytest.mark.asyncio
async def test_get_agent_id_for_slug_404_returns_none():
    """Slug misses are common (typos, reserved subdomains like www./api.)
    — return None instead of raising so the resolver branch is cheap."""
    from surogates.runtime import PlatformClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not bound"})

    client = PlatformClient(
        base_url="https://ops.example.com",
        token="t",
        transport=_mock_transport(handler),
    )
    try:
        result = await client.get_agent_id_for_slug("nope")
    finally:
        await client.aclose()
    assert result is None


@pytest.mark.asyncio
async def test_get_agent_id_for_slug_401_raises_platform_auth_error():
    """A bad/revoked runtime token is an operations problem, not a slug
    miss — surface distinctly so monitoring can page."""
    from surogates.runtime import PlatformClient
    from surogates.runtime.platform_client import PlatformAuthError

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid"})

    client = PlatformClient(
        base_url="https://ops.example.com",
        token="bad",
        transport=_mock_transport(handler),
    )
    try:
        with pytest.raises(PlatformAuthError):
            await client.get_agent_id_for_slug("acme")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_agent_id_for_slug_500_propagates_http_error():
    from surogates.runtime import PlatformClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = PlatformClient(
        base_url="https://ops.example.com",
        token="t",
        transport=_mock_transport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_agent_id_for_slug("acme")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_aclose_releases_underlying_client():
    """A closed client refuses subsequent requests."""
    from surogates.runtime import PlatformClient

    def handler(_request):  # pragma: no cover — should never be called post-close
        return httpx.Response(200, json={})

    client = PlatformClient(
        base_url="https://ops.example.com",
        token="x",
        transport=_mock_transport(handler),
    )
    await client.aclose()
    with pytest.raises(RuntimeError):
        await client.get_runtime_config("a-1")
