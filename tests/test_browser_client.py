"""Tests for surogates.browser.client.KernelBrowserClient."""

from __future__ import annotations

import json
import base64
import time
from typing import Any

import httpx
import pytest

from surogates.browser.client import KernelBrowserClient

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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
            return httpx.Response(
                404, json={"error": "not found", "path": request.url.path}
            )

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

    async def test_navigate_propagates_kernel_error(
        self, client_with_transport
    ) -> None:
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

    async def test_navigate_invalidates_snapshot_cache(
        self, client_with_transport
    ) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e1"] = {
            "x": 10,
            "y": 10,
            "role": "button",
            "name": "x",
        }
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
        await client.navigate("https://example.com")
        assert client._snapshot_cache == {}


class TestGetState:
    async def test_get_state_executes_dom_snapshot_inside_page_evaluate(self) -> None:
        captured: list[dict[str, Any]] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                captured.append(json.loads(request.content))
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "result": {
                            "url": "u",
                            "title": "t",
                            "viewport": {"width": 1, "height": 1},
                            "nodes": [],
                        },
                    },
                )

        client = KernelBrowserClient(rest_url="http://browser-test:10001")
        client._http = httpx.AsyncClient(
            base_url=client.rest_url,
            transport=CapturingTransport(),
        )

        await client.get_state()

        code = captured[0]["code"]
        assert "await page.evaluate" in code
        assert code.index("await page.evaluate") < code.index("document.querySelector")
        assert code.index("await page.evaluate") < code.index("root.querySelectorAll")

    async def test_get_state_returns_tree_with_refs(
        self, client_with_transport
    ) -> None:
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

    async def test_get_state_populates_snapshot_cache(
        self, client_with_transport
    ) -> None:
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
                                "backend_node_id": 42,
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
        assert cached["backend_node_id"] == 42
        assert cached["nth"] == 0

    async def test_get_state_assigns_backend_node_id_and_nth(
        self, client_with_transport
    ) -> None:
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
                                "name": "Add",
                                "x": 0,
                                "y": 0,
                                "width": 10,
                                "height": 10,
                                "backend_node_id": 7,
                            },
                            {
                                "role": "button",
                                "name": "Add",
                                "x": 20,
                                "y": 0,
                                "width": 10,
                                "height": 10,
                                "backend_node_id": 9,
                            },
                            {
                                "role": "link",
                                "name": "Add",
                                "x": 40,
                                "y": 0,
                                "width": 10,
                                "height": 10,
                            },
                        ],
                    },
                },
            )
        )
        await client.get_state()
        # nth disambiguates duplicate (role, name) pairs; a different role
        # restarts the counter.
        assert client._snapshot_cache["@e1"]["nth"] == 0
        assert client._snapshot_cache["@e1"]["backend_node_id"] == 7
        assert client._snapshot_cache["@e2"]["nth"] == 1
        assert client._snapshot_cache["@e2"]["backend_node_id"] == 9
        assert client._snapshot_cache["@e3"]["nth"] == 0
        # A missing backend_node_id is preserved as None for the fallback path.
        assert client._snapshot_cache["@e3"]["backend_node_id"] is None

    async def test_get_state_overwrites_old_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e9"] = {
            "x": 0,
            "y": 0,
            "role": "stale",
            "name": "stale",
        }
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
                    {
                        "role": "generic",
                        "name": "",
                        "x": 0,
                        "y": 0,
                        "width": 0,
                        "height": 0,
                    },
                    {
                        "role": "button",
                        "name": "Go",
                        "x": 10,
                        "y": 10,
                        "width": 1,
                        "height": 1,
                    },
                    {
                        "role": "paragraph",
                        "name": "",
                        "x": 0,
                        "y": 20,
                        "width": 0,
                        "height": 0,
                    },
                    {
                        "role": "link",
                        "name": "Home",
                        "x": 30,
                        "y": 30,
                        "width": 1,
                        "height": 1,
                    },
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

    async def test_filters_dont_corrupt_cache(
        self, client_with_transport, deep_response
    ) -> None:
        client, handlers = client_with_transport
        handlers.append(("POST", "/playwright/execute", 200, deep_response))
        await client.get_state(interactive_only=True)
        assert set(client._snapshot_cache.keys()) == {"@e1", "@e2", "@e3", "@e4"}

    async def test_max_depth_truncates(
        self, client_with_transport, deep_response
    ) -> None:
        client, handlers = client_with_transport
        deep_response = {
            "success": True,
            "result": {
                **deep_response["result"],
                "nodes": [
                    {**node, "depth": depth}
                    for node, depth in zip(
                        deep_response["result"]["nodes"], [0, 1, 1, 3]
                    )
                ],
            },
        }
        handlers.append(("POST", "/playwright/execute", 200, deep_response))
        state = await client.get_state(max_depth=2)
        assert len(state["tree"]) == 3
        assert all(node["ref"] != "@e4" for node in state["tree"])

    async def test_consent_actions_are_promoted_and_survive_depth_limits(
        self, client_with_transport
    ) -> None:
        client, handlers = client_with_transport
        handlers.append(
            (
                "POST",
                "/playwright/execute",
                200,
                {
                    "success": True,
                    "result": {
                        "url": "https://example.test/",
                        "title": "Example",
                        "viewport": {"width": 1280, "height": 720},
                        "nodes": [
                            {
                                "role": "link",
                                "name": "Economy",
                                "x": 100,
                                "y": 20,
                                "width": 80,
                                "height": 30,
                                "depth": 4,
                            },
                            {
                                "role": "button",
                                "name": "ACCEPT TOATE",
                                "x": 1000,
                                "y": 640,
                                "width": 180,
                                "height": 40,
                                "depth": 14,
                            },
                            {
                                "role": "button",
                                "name": "VREAU SA MODIFIC SETARILE INDIVIDUAL",
                                "x": 1000,
                                "y": 690,
                                "width": 180,
                                "height": 40,
                                "depth": 14,
                            },
                        ],
                    },
                },
            )
        )

        state = await client.get_state(interactive_only=True, max_depth=5)

        assert state["tree"][0]["ref"] == "@e2"
        assert state["tree"][0]["name"] == "ACCEPT TOATE"
        assert state["tree"][0]["intent"] == "accept_consent"
        assert state["tree"][1]["ref"] == "@e1"
        assert "VREAU SA MODIFIC SETARILE INDIVIDUAL" not in {
            node["name"] for node in state["tree"]
        }


class TestClickType:
    async def test_click_at_coords(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e1"] = {"x": 1, "y": 1, "role": "button", "name": "Go"}
        handlers.append(
            ("POST", "/playwright/execute", 200, {"success": True, "result": True})
        )
        await client.click_at(120, 240)
        # Explicit-coordinate clicks no longer invalidate cached refs; only
        # navigation does.
        assert client._snapshot_cache == {"@e1": {"x": 1, "y": 1, "role": "button", "name": "Go"}}

    async def test_click_ref_resolves_via_cdp_at_action_time(
        self, client_with_transport
    ) -> None:
        client, _handlers = client_with_transport
        # The cached center (50/60) is stale on a dynamic page; the live center
        # is recomputed via CDP, so the JS must resolve by backend node id and
        # never click the frozen coordinates.
        client._snapshot_cache["@e3"] = {
            "x": 50,
            "y": 60,
            "role": "button",
            "name": "Go",
            "backend_node_id": 314,
            "nth": 0,
        }
        captured: list[dict[str, Any]] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                captured.append({
                    "path": request.url.path,
                    "body": json.loads(request.content),
                })
                return httpx.Response(200, json={"success": True, "result": True})

        client._http = httpx.AsyncClient(
            base_url=client.rest_url, transport=CapturingTransport()
        )
        await client.click_ref("@e3")
        assert captured[0]["path"] == "/playwright/execute"
        code = captured[0]["body"]["code"]
        assert "newCDPSession" in code
        assert "DOM.resolveNode" in code
        assert "backendNodeId: __bid" in code
        assert "314" in code
        assert "Accessibility.getFullAXTree" in code
        assert "elementFromPoint" in code
        assert "page.mouse.click(res.cx, res.cy" in code
        # The ref path must not invalidate the cache so a follow-up type works.
        assert "@e3" in client._snapshot_cache

    async def test_click_ref_passes_button_and_num_clicks(
        self, client_with_transport
    ) -> None:
        client, _handlers = client_with_transport
        client._snapshot_cache["@e1"] = {
            "x": 1,
            "y": 1,
            "role": "button",
            "name": "Go",
            "backend_node_id": 5,
            "nth": 0,
        }
        captured: list[dict[str, Any]] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                captured.append({"body": json.loads(request.content)})
                return httpx.Response(200, json={"success": True, "result": True})

        client._http = httpx.AsyncClient(
            base_url=client.rest_url, transport=CapturingTransport()
        )
        await client.click_ref("@e1", button="right", num_clicks=2)
        code = captured[0]["body"]["code"]
        assert '"right"' in code
        assert "clickCount: 2" in code

    async def test_ref_fallback_uses_role_name_nth(
        self, client_with_transport
    ) -> None:
        client, _handlers = client_with_transport
        # A null backend node id forces the role/name/nth healing path.
        client._snapshot_cache["@e2"] = {
            "x": 5,
            "y": 5,
            "role": "link",
            "name": "Profile",
            "backend_node_id": None,
            "nth": 3,
        }
        captured: list[dict[str, Any]] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                captured.append({"body": json.loads(request.content)})
                return httpx.Response(200, json={"success": True, "result": True})

        client._http = httpx.AsyncClient(
            base_url=client.rest_url, transport=CapturingTransport()
        )
        await client.click_ref("@e2")
        code = captured[0]["body"]["code"]
        # backend node id is null -> the fast-path resolveNode is gated off.
        assert "const __bid = null;" in code
        assert "Accessibility.getFullAXTree" in code
        assert '"link"' in code
        assert '"Profile"' in code
        assert "matches[3]" in code

    async def test_click_ref_unknown_raises(self, client_with_transport) -> None:
        client, _ = client_with_transport
        with pytest.raises(KeyError, match="@e99"):
            await client.click_ref("@e99")

    async def test_click_waits_for_network_after_request(
        self, client_with_transport
    ) -> None:
        client, _ = client_with_transport
        captured: list[dict[str, Any]] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                captured.append({"body": json.loads(request.content)})
                return httpx.Response(200, json={"success": True, "result": True})

        client._http = httpx.AsyncClient(
            base_url=client.rest_url, transport=CapturingTransport()
        )
        await client.click_at(10, 20)
        code = captured[0]["body"]["code"]
        assert "page.on('request'" in code
        assert "page.mouse.click" in code
        assert "waitForLoadState('networkidle'" in code
        assert "page.off('request'" in code

    async def test_click_down_does_not_wait_for_network(
        self, client_with_transport
    ) -> None:
        client, _ = client_with_transport
        captured: list[dict[str, Any]] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                captured.append({"body": json.loads(request.content)})
                return httpx.Response(200, json={"success": True, "result": True})

        client._http = httpx.AsyncClient(
            base_url=client.rest_url, transport=CapturingTransport()
        )
        await client.click_at(5, 6, click_type="down")
        code = captured[0]["body"]["code"]
        assert "page.mouse.down" in code
        assert "waitForLoadState" not in code

    async def test_type_text_keeps_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e1"] = {
            "x": 1,
            "y": 1,
            "role": "textbox",
            "name": "Email",
        }
        handlers.append(
            ("POST", "/playwright/execute", 200, {"success": True, "result": True})
        )
        await client.type_text("hello")
        # Typing must not invalidate refs; a click→type on the same ref has to
        # keep working without a fresh browser_get_state.
        assert "@e1" in client._snapshot_cache

    async def test_type_text_uses_cdp_keyboard(self, client_with_transport) -> None:
        client, _handlers = client_with_transport
        captured: list[dict[str, Any]] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                captured.append(
                    {"path": request.url.path, "body": json.loads(request.content)}
                )
                return httpx.Response(200, json={"success": True, "result": True})

        client._http = httpx.AsyncClient(
            base_url=client.rest_url, transport=CapturingTransport()
        )
        await client.type_text("hi there")
        # The kernel /computer/type endpoint is bypassed entirely: OS-level
        # keystrokes never reach contenteditable editors, so typing must go
        # through Playwright's CDP keyboard.
        assert captured[0]["path"] == "/playwright/execute"
        assert "keyboard.type" in captured[0]["body"]["code"]
        assert "hi there" in captured[0]["body"]["code"]

    async def test_type_into_ref_clicks_via_cdp_then_types(
        self, client_with_transport
    ) -> None:
        client, _handlers = client_with_transport
        client._snapshot_cache["@e2"] = {
            "x": 30,
            "y": 40,
            "role": "textbox",
            "name": "Email",
            "backend_node_id": 88,
            "nth": 0,
        }
        seen: list[dict[str, Any]] = []

        class TracingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                body = (
                    json.loads(request.content) if request.content else {}
                )
                seen.append({"path": request.url.path, "body": body})
                if request.url.path == "/playwright/execute":
                    return httpx.Response(200, json={"success": True, "result": True})
                return httpx.Response(200, json={"ok": True})

        client._http = httpx.AsyncClient(
            base_url=client.rest_url, transport=TracingTransport()
        )
        await client.type_into_ref("@e2", "test@example.com")
        # First a CDP click to focus the field, then a CDP keyboard type.
        assert seen[0]["path"] == "/playwright/execute"
        assert "newCDPSession" in seen[0]["body"]["code"]
        assert seen[1]["path"] == "/playwright/execute"
        assert "keyboard.type" in seen[1]["body"]["code"]
        assert "test@example.com" in seen[1]["body"]["code"]
        # The ref must survive so a follow-up action can reuse it.
        assert "@e2" in client._snapshot_cache


class TestSmallActions:
    async def test_press_key_keeps_cache(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e1"] = {"x": 1, "y": 1, "role": "button", "name": "Go"}
        handlers.append(
            ("POST", "/playwright/execute", 200, {"success": True, "result": True})
        )
        await client.press_key("Enter")
        # Pressing a key must not invalidate refs.
        assert "@e1" in client._snapshot_cache

    async def test_press_key_chord(self, client_with_transport) -> None:
        client, _handlers = client_with_transport
        captured: list[dict[str, Any]] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                captured.append(json.loads(request.content))
                return httpx.Response(200, json={"success": True, "result": True})

        client._http = httpx.AsyncClient(
            base_url=client.rest_url, transport=CapturingTransport()
        )
        # Casual modifier names normalize to Playwright's vocabulary and the
        # keys join into a single chord pressed via the CDP keyboard.
        await client.press_key("Ctrl", "a")
        code = captured[0]["code"]
        assert "keyboard.press" in code
        assert "Control+a" in code

    async def test_scroll_at(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e1"] = {"x": 1, "y": 1, "role": "button", "name": "Go"}
        handlers.append(
            (
                "POST",
                "/playwright/execute",
                200,
                {
                    "success": True,
                    "result": {
                        "scroll_x": 0,
                        "scroll_y": 300,
                        "page_height": 4000,
                        "viewport_height": 720,
                    },
                },
            )
        )
        position = await client.scroll_at(640, 400, delta_y=300)
        assert position["scroll_y"] == 300
        # Scrolling preserves refs: elements (and their backend node ids)
        # persist and a ref's click center is recomputed at action time, so a
        # scroll no longer forces a re-snapshot. Only navigation invalidates.
        assert "@e1" in client._snapshot_cache

    async def test_drag(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        client._snapshot_cache["@e1"] = {"x": 1, "y": 1, "role": "button", "name": "Go"}
        handlers.append(("POST", "/computer/drag_mouse", 200, {"ok": True}))
        await client.drag([(10, 10), (200, 200)])
        assert client._snapshot_cache == {}

    async def test_wait_sleeps(self, client_with_transport) -> None:
        client, _ = client_with_transport
        start = time.perf_counter()
        await client.wait(50)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert 40 <= elapsed_ms < 500
        assert client._snapshot_cache == {}


class TestScreenshot:
    async def test_screenshot_returns_png_bytes(self, client_with_transport) -> None:
        client, handlers = client_with_transport
        handlers.append(
            ("POST", "/computer/screenshot", 200, PNG_MAGIC + b"fakepngbody")
        )
        result = await client.screenshot()
        assert result["png_bytes"].startswith(PNG_MAGIC)
        assert "annotations" not in result

    async def test_screenshot_can_capture_page_viewport_without_desktop(
        self,
        client_with_transport,
    ) -> None:
        client, _handlers = client_with_transport
        seen_paths: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            assert request.url.path == "/playwright/execute"
            payload = json.loads(request.content.decode())
            assert "page.screenshot(options)" in payload["code"]
            assert '"path"' not in payload["code"]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": base64.b64encode(PNG_MAGIC + b"viewport").decode(),
                },
            )

        client._http = httpx.AsyncClient(
            base_url=client.rest_url,
            transport=httpx.MockTransport(handler),
        )

        result = await client.screenshot(viewport_only=True)

        assert result["png_bytes"] == PNG_MAGIC + b"viewport"
        assert seen_paths == ["/playwright/execute"]

    async def test_screenshot_can_save_inside_browser_workspace(
        self,
        client_with_transport,
    ) -> None:
        client, _handlers = client_with_transport

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/playwright/execute"
            payload = json.loads(request.content.decode())
            assert "/workspace/browser-screenshots/capture.png" in payload["code"]
            assert "toString('base64')" in payload["code"]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": base64.b64encode(PNG_MAGIC + b"saved").decode(),
                },
            )

        client._http = httpx.AsyncClient(
            base_url=client.rest_url,
            transport=httpx.MockTransport(handler),
        )

        result = await client.screenshot(
            save_path="/workspace/browser-screenshots/capture.png",
        )

        assert result["png_bytes"] == PNG_MAGIC + b"saved"

    async def test_screenshot_with_annotate_runs_overlay_then_clears(
        self, client_with_transport
    ) -> None:
        client, _ = client_with_transport
        client._snapshot_cache["@e1"] = {
            "x": 100,
            "y": 50,
            "role": "button",
            "name": "Go",
        }
        client._snapshot_cache["@e2"] = {
            "x": 200,
            "y": 50,
            "role": "link",
            "name": "Help",
        }
        seen_paths: list[str] = []

        class TracingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                seen_paths.append(request.url.path)
                if request.url.path == "/computer/screenshot":
                    return httpx.Response(200, content=PNG_MAGIC + b"img")
                if request.url.path == "/playwright/execute":
                    return httpx.Response(200, json={"success": True, "result": True})
                return httpx.Response(404)

        client._http = httpx.AsyncClient(
            base_url=client.rest_url, transport=TracingTransport()
        )
        result = await client.screenshot(annotate=True)

        assert seen_paths.count("/playwright/execute") == 2
        assert seen_paths.count("/computer/screenshot") == 1
        assert result["annotations"] == [
            {"ref": "@e1", "label": 1, "role": "button", "name": "Go"},
            {"ref": "@e2", "label": 2, "role": "link", "name": "Help"},
        ]
