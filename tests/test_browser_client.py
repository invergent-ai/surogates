"""Tests for surogates.browser.client.KernelBrowserClient."""

from __future__ import annotations

import json

import httpx
import pytest

from surogates.browser.client import KernelBrowserClient


@pytest.fixture()
def mock_transport():
    """Build a list of ``(method, path, status, body)`` handlers."""

    handlers: list[tuple] = []

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            for method, path, status, body in handlers:
                if request.method == method and request.url.path == path:
                    if isinstance(body, bytes):
                        return httpx.Response(status, content=body)
                    return httpx.Response(status, json=body)
            return httpx.Response(404, json={"error": "not found", "path": request.url.path})

    return handlers, MockTransport()


@pytest.fixture()
def client_with_transport(mock_transport):
    """Create a KernelBrowserClient using a mock transport."""

    handlers, transport = mock_transport
    client = KernelBrowserClient(rest_url="http://browser-test:10001")
    client._http = httpx.AsyncClient(
        base_url="http://browser-test:10001",
        transport=transport,
        timeout=5.0,
    )
    return client, handlers


class TestClientLifecycle:
    async def test_close_disposes_http(self) -> None:
        client = KernelBrowserClient(rest_url="http://x:10001")
        await client.close()
        assert client._closed is True

    async def test_context_manager_closes(self) -> None:
        async with KernelBrowserClient(rest_url="http://x:10001") as client:
            assert client._closed is False
        assert client._closed is True

    async def test_double_close_is_noop(self) -> None:
        client = KernelBrowserClient(rest_url="http://x:10001")
        await client.close()
        await client.close()
        assert client._closed is True

    async def test_rest_url_is_normalized(self) -> None:
        client = KernelBrowserClient(rest_url="http://x:10001/")
        assert client.rest_url == "http://x:10001"
        await client.close()


class TestNavigate:
    async def test_navigate_returns_url_and_title(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append(
            (
                "POST",
                "/playwright/execute",
                200,
                {
                    "success": True,
                    "result": {"url": "https://example.com/", "title": "Example"},
                },
            )
        )
        result = await client.navigate("https://example.com")
        assert result["url"] == "https://example.com/"
        assert result["title"] == "Example"

    async def test_navigate_propagates_kernel_error(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append(
            (
                "POST",
                "/playwright/execute",
                200,
                {"success": False, "error": "ERR_NAME_NOT_RESOLVED"},
            )
        )
        with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
            await client.navigate("https://nope.invalid")

    async def test_navigate_invalidates_snapshot_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e1"] = {"x": 10, "y": 10, "role": "button", "name": "x"}
        handlers.append(
            (
                "POST",
                "/playwright/execute",
                200,
                {"success": True, "result": {"url": "https://example.com/", "title": "Example"}},
            )
        )
        await client.navigate("https://example.com")
        assert client._snapshot_cache == {}
