"""Tests for surogates.browser.client.KernelBrowserClient."""

from __future__ import annotations

import json
from typing import Any

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


class TestGetState:
    async def test_get_state_returns_tree_with_refs(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append(
            (
                "POST",
                "/playwright/execute",
                200,
                {
                    "success": True,
                    "result": {
                        "url": "https://example.com/",
                        "title": "Example",
                        "viewport": {"width": 1280, "height": 800},
                        "nodes": [
                            {
                                "role": "link",
                                "name": "Settings",
                                "x": 1130,
                                "y": 24,
                                "width": 80,
                                "height": 32,
                            },
                            {
                                "role": "button",
                                "name": "New project",
                                "x": 200,
                                "y": 80,
                                "width": 120,
                                "height": 36,
                            },
                        ],
                    },
                },
            )
        )
        state = await client.get_state()
        assert state["url"] == "https://example.com/"
        assert state["viewport"] == {"width": 1280, "height": 800}
        assert state["tree"][0]["ref"] == "@e1"
        assert state["tree"][0]["role"] == "link"
        assert state["tree"][0]["name"] == "Settings"
        assert state["tree"][1]["ref"] == "@e2"

    async def test_get_state_populates_snapshot_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append(
            (
                "POST",
                "/playwright/execute",
                200,
                {
                    "success": True,
                    "result": {
                        "url": "u",
                        "title": "t",
                        "viewport": {"width": 100, "height": 100},
                        "nodes": [
                            {
                                "role": "button",
                                "name": "Go",
                                "x": 10,
                                "y": 20,
                                "width": 50,
                                "height": 30,
                            }
                        ],
                    },
                },
            )
        )
        await client.get_state()
        cached = client._snapshot_cache["@e1"]
        assert cached["x"] == 10 + 50 // 2
        assert cached["y"] == 20 + 30 // 2
        assert cached["role"] == "button"
        assert cached["name"] == "Go"

    async def test_get_state_overwrites_old_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e9"] = {"x": 0, "y": 0, "role": "stale", "name": "stale"}
        handlers.append(
            (
                "POST",
                "/playwright/execute",
                200,
                {
                    "success": True,
                    "result": {
                        "url": "u",
                        "title": "t",
                        "viewport": {"width": 1, "height": 1},
                        "nodes": [
                            {
                                "role": "button",
                                "name": "fresh",
                                "x": 1,
                                "y": 1,
                                "width": 0,
                                "height": 0,
                            }
                        ],
                    },
                },
            )
        )
        await client.get_state()
        assert "@e9" not in client._snapshot_cache
        assert "@e1" in client._snapshot_cache


class TestGetStateFilters:
    @pytest.fixture()
    def deep_response(self) -> dict[str, Any]:
        return {
            "success": True,
            "result": {
                "url": "u",
                "title": "t",
                "viewport": {"width": 1, "height": 1},
                "nodes": [
                    {"role": "generic", "name": "", "x": 0, "y": 0, "width": 0, "height": 0},
                    {"role": "button", "name": "Go", "x": 10, "y": 10, "width": 1, "height": 1},
                    {"role": "paragraph", "name": "", "x": 0, "y": 20, "width": 0, "height": 0},
                    {"role": "link", "name": "Home", "x": 30, "y": 30, "width": 1, "height": 1},
                ],
            },
        }

    async def test_interactive_only_drops_structural_nodes(
        self, client_with_transport, deep_response
    ) -> None:
        client, handlers = client_with_transport
        handlers.append(("POST", "/playwright/execute", 200, deep_response))
        state = await client.get_state(interactive_only=True)
        roles = {node["role"] for node in state["tree"]}
        assert roles == {"button", "link"}
        assert state["tree"][0]["ref"] == "@e2"
        assert state["tree"][1]["ref"] == "@e4"

    async def test_filters_dont_corrupt_cache(self, client_with_transport, deep_response) -> None:
        client, handlers = client_with_transport
        handlers.append(("POST", "/playwright/execute", 200, deep_response))
        await client.get_state(interactive_only=True)
        assert set(client._snapshot_cache.keys()) == {"@e1", "@e2", "@e3", "@e4"}

    async def test_max_depth_truncates(self, client_with_transport, deep_response) -> None:
        client, handlers = client_with_transport
        deep_response = {
            "success": True,
            "result": {
                **deep_response["result"],
                "nodes": [
                    {**node, "depth": depth}
                    for node, depth in zip(deep_response["result"]["nodes"], [0, 1, 1, 3])
                ],
            },
        }
        handlers.append(("POST", "/playwright/execute", 200, deep_response))
        state = await client.get_state(max_depth=2)
        assert len(state["tree"]) == 3
        assert all(node["ref"] != "@e4" for node in state["tree"])
