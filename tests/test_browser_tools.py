"""Tests for surogates.tools.builtin.browser handlers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from surogates.browser.base import BrowserEndpoint, BrowserSpec
from surogates.browser.control import ControlEntry
from surogates.browser.pool import EnsureResult


class FakePool:
    def __init__(self) -> None:
        self.ensures: list[tuple[str, str, str]] = []
        self.specs: list[BrowserSpec] = []
        self.destroyed: list[str] = []
        self._fixed_endpoint = BrowserEndpoint(
            rest_url="http://browser:30000",
            cdp_url="ws://browser:31000",
            live_view_url="ws://browser:32000",
        )
        self.snapshot_cache: dict[str, dict[str, Any]] = {}

    async def ensure(
        self,
        session_id: str,
        org_id: str,
        user_id: str,
        spec: BrowserSpec,
    ) -> EnsureResult:
        self.ensures.append((session_id, org_id, user_id))
        self.specs.append(spec)
        return EnsureResult(
            browser_id="b1",
            endpoint=self._fixed_endpoint,
            newly_provisioned=True,
            snapshot_cache=self.snapshot_cache,
        )

    async def destroy_for_session(self, session_id: str) -> None:
        self.destroyed.append(session_id)


class FakeControlStore:
    def __init__(self, holder: str | None = None) -> None:
        self._holder = holder

    async def get(self, session_id: str) -> ControlEntry | None:
        if self._holder is None:
            return None
        return ControlEntry(owner_user_id=self._holder, acquired_at=datetime.now(timezone.utc))


class FakeClient:
    def __init__(self) -> None:
        self.navigated_to: str | None = None
        self.closed = False

    async def navigate(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.navigated_to = url
        return {"url": url, "title": "Test Page"}

    async def get_state(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "url": "http://example.com/",
            "title": "Test",
            "viewport": {"width": 1, "height": 1},
            "tree": [],
        }

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


class FailingStateClient(FakeClient):
    async def get_state(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("document is not defined")


class FakeClickClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict[str, Any]]] = []

    async def click_at(self, x: int, y: int, **kwargs: Any) -> None:
        self.calls.append(("click_at", (x, y), kwargs))

    async def click_ref(self, ref: str, **kwargs: Any) -> None:
        self.calls.append(("click_ref", (ref,), kwargs))

    async def type_text(self, text: str, **kwargs: Any) -> None:
        self.calls.append(("type_text", (text,), kwargs))

    async def type_into_ref(self, ref: str, text: str, **kwargs: Any) -> None:
        self.calls.append(("type_into_ref", (ref, text), kwargs))

    async def press_key(self, *keys: str, **kwargs: Any) -> None:
        self.calls.append(("press_key", keys, kwargs))

    async def scroll_at(self, x: int, y: int, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("scroll_at", (x, y), kwargs))
        return {
            "scroll_x": 0,
            "scroll_y": 300,
            "page_height": 4000,
            "viewport_height": 720,
        }

    async def drag(self, path: list[tuple[int, int]], **kwargs: Any) -> None:
        self.calls.append(("drag", (tuple(path),), kwargs))

    async def wait(self, ms: int) -> None:
        self.calls.append(("wait", (ms,), {}))

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "FakeClickClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass


class FakeScreenshotClient:
    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []

    async def screenshot(
        self,
        *,
        region: dict[str, int] | None = None,
        annotate: bool = False,
        save_path: str | None = None,
    ) -> dict[str, Any]:
        if save_path is not None:
            raise RuntimeError("save_path unsupported by fake")
        self.captured.append({"region": region, "annotate": annotate})
        result: dict[str, Any] = {"png_bytes": b"\x89PNG\r\n\x1a\nimg"}
        if annotate:
            result["annotations"] = [
                {"ref": "@e1", "label": 1, "role": "button", "name": "Go"},
            ]
        return result

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "FakeScreenshotClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass


class FakeLargeScreenshotClient(FakeScreenshotClient):
    async def screenshot(
        self,
        *,
        region: dict[str, int] | None = None,
        annotate: bool = False,
        save_path: str | None = None,
    ) -> dict[str, Any]:
        if save_path is not None:
            raise RuntimeError("save_path unsupported by fake")
        self.captured.append({"region": region, "annotate": annotate})
        return {"png_bytes": b"\x89PNG\r\n\x1a\n" + (b"x" * 300_000)}


class FakeSavePathScreenshotClient(FakeScreenshotClient):
    async def screenshot(
        self,
        *,
        region: dict[str, int] | None = None,
        annotate: bool = False,
        save_path: str | None = None,
    ) -> dict[str, Any]:
        self.captured.append(
            {"region": region, "annotate": annotate, "save_path": save_path}
        )
        return {"png_bytes": b"\x89PNG\r\n\x1a\n" + (b"x" * 300_000)}


class FakeStorage:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, bytes]] = []

    async def write(self, bucket: str, key: str, data: bytes) -> None:
        self.writes.append((bucket, key, data))


@pytest.fixture()
def tenant():
    return SimpleNamespace(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
    )


class TestNavigateHandler:
    async def test_navigates_via_pool(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_navigate_handler

        pool = FakePool()
        client = FakeClient()
        control = FakeControlStore()
        sid = uuid4()

        result = await _browser_navigate_handler(
            {"url": "https://example.com"},
            tenant=tenant,
            session_id=sid,
            browser_pool=pool,
            browser_control=control,
            _client_factory=lambda endpoint: client,
        )
        body = json.loads(result)
        assert body["url"] == "https://example.com"
        assert body["title"] == "Test Page"
        assert pool.ensures == [(str(sid), str(tenant.org_id), str(tenant.user_id))]
        assert client.navigated_to == "https://example.com"

    async def test_passes_workspace_mount_spec(self, tenant, tmp_path) -> None:
        from surogates.tools.builtin.browser import _browser_navigate_handler

        pool = FakePool()
        session_id = uuid4()
        await _browser_navigate_handler(
            {"url": "https://example.com"},
            tenant=tenant,
            session_id=session_id,
            browser_pool=pool,
            browser_control=FakeControlStore(),
            workspace_path=str(tmp_path),
            session_config={"storage_bucket": "agent-bucket"},
            _client_factory=lambda endpoint: FakeClient(),
        )

        assert pool.specs[0].workspace_path == str(tmp_path)
        assert (
            pool.specs[0].workspace_source_ref
            == f"s3://agent-bucket/{session_id}"
        )

    async def test_short_circuits_when_user_in_control(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_navigate_handler

        pool = FakePool()
        result = await _browser_navigate_handler(
            {"url": "https://example.com"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=pool,
            browser_control=FakeControlStore(holder="other-user"),
            _client_factory=lambda endpoint: FakeClient(),
        )
        body = json.loads(result)
        assert body["error"] == "paused_by_user"
        assert pool.ensures == []

    async def test_returns_unavailable_when_pool_missing(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_navigate_handler

        result = await _browser_navigate_handler(
            {"url": "https://example.com"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=None,
            browser_control=None,
        )
        body = json.loads(result)
        assert body["error"] == "browser_unavailable"


class TestGetStateHandler:
    async def test_returns_tree(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_get_state_handler

        result = await _browser_get_state_handler(
            {"interactive_only": True},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeClient(),
        )
        body = json.loads(result)
        assert "tree" in body
        assert body["url"] == "http://example.com/"

    async def test_returns_structured_error_when_state_snapshot_fails(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_get_state_handler

        result = await _browser_get_state_handler(
            {},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FailingStateClient(),
        )

        body = json.loads(result)
        assert body == {
            "error": "get_state_failed",
            "detail": "document is not defined",
        }


class TestCloseHandler:
    async def test_destroys_session_browser(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_close_handler

        pool = FakePool()
        sid = uuid4()
        result = await _browser_close_handler(
            {},
            tenant=tenant,
            session_id=sid,
            browser_pool=pool,
            browser_control=FakeControlStore(),
        )
        body = json.loads(result)
        assert body["closed"] is True
        assert pool.destroyed == [str(sid)]


class TestClickHandler:
    async def test_click_with_ref(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_click_handler

        client = FakeClickClient()
        await _browser_click_handler(
            {"ref": "@e3"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: client,
        )
        assert client.calls[0][0] == "click_ref"
        assert client.calls[0][1] == ("@e3",)

    async def test_click_with_coords(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_click_handler

        client = FakeClickClient()
        await _browser_click_handler(
            {"x": 100, "y": 200},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: client,
        )
        assert client.calls[0][0] == "click_at"
        assert client.calls[0][1] == (100, 200)

    async def test_click_requires_ref_or_coords(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_click_handler

        result = await _browser_click_handler(
            {},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeClickClient(),
        )
        assert json.loads(result)["error"] == "invalid_arguments"


class TestTypeHandler:
    async def test_type_into_ref(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_type_handler

        client = FakeClickClient()
        await _browser_type_handler(
            {"ref": "@e2", "text": "hello"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: client,
        )
        assert client.calls[0][0] == "type_into_ref"
        assert client.calls[0][1] == ("@e2", "hello")

    async def test_type_at_focus(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_type_handler

        client = FakeClickClient()
        await _browser_type_handler(
            {"text": "fallback"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: client,
        )
        assert client.calls[0][0] == "type_text"


class TestPressKey:
    async def test_press_single(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_press_key_handler

        client = FakeClickClient()
        await _browser_press_key_handler(
            {"keys": ["Enter"]},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: client,
        )
        assert client.calls[0][0] == "press_key"
        assert client.calls[0][1] == ("Enter",)


class TestScrollDragWait:
    async def test_scroll(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_scroll_handler

        client = FakeClickClient()
        result = json.loads(
            await _browser_scroll_handler(
                {"x": 100, "y": 200, "delta_y": 300},
                tenant=tenant,
                session_id=uuid4(),
                browser_pool=FakePool(),
                browser_control=FakeControlStore(),
                _client_factory=lambda endpoint: client,
            )
        )
        assert client.calls[0][0] == "scroll_at"
        assert client.calls[0][2]["delta_y"] == 300
        # Position feedback lets the model see where it landed instead
        # of blindly re-scrolling an unmoving page.
        assert result["scroll_y"] == 300
        assert result["at_bottom"] is False

    async def test_scroll_reports_bottom(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_scroll_handler

        class BottomClient(FakeClickClient):
            async def scroll_at(self, x: int, y: int, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(("scroll_at", (x, y), kwargs))
                return {
                    "scroll_x": 0,
                    "scroll_y": 3280,
                    "page_height": 4000,
                    "viewport_height": 720,
                }

        client = BottomClient()
        result = json.loads(
            await _browser_scroll_handler(
                {"x": 100, "y": 200, "delta_y": 2000},
                tenant=tenant,
                session_id=uuid4(),
                browser_pool=FakePool(),
                browser_control=FakeControlStore(),
                _client_factory=lambda endpoint: client,
            )
        )
        assert result["at_bottom"] is True

    async def test_drag(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_drag_handler

        client = FakeClickClient()
        await _browser_drag_handler(
            {"path": [[10, 10], [200, 200]]},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: client,
        )
        assert client.calls[0][0] == "drag"
        assert client.calls[0][1] == (((10, 10), (200, 200)),)

    async def test_wait_caps_at_30s(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_wait_handler

        client = FakeClickClient()
        await _browser_wait_handler(
            {"ms": 999_999},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: client,
        )
        assert client.calls[0][0] == "wait"
        assert client.calls[0][1] == (30_000,)


class TestScreenshotHandler:
    async def test_saves_png_to_workspace(self, tenant, tmp_path) -> None:
        from surogates.tools.builtin.browser import _browser_screenshot_handler

        result = await _browser_screenshot_handler(
            {},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            workspace_path=str(tmp_path),
            _client_factory=lambda endpoint: FakeScreenshotClient(),
        )
        body = json.loads(result)
        assert body["saved"] is True
        assert body["relative_path"].startswith("browser-screenshots/")
        assert body["relative_path"].endswith(".png")
        assert body["path"] == str(tmp_path / body["relative_path"])
        assert "base64" not in body
        assert "error" not in body
        assert (tmp_path / body["relative_path"]).read_bytes() == b"\x89PNG\r\n\x1a\nimg"

    async def test_oversized_png_is_still_saved_to_workspace(
        self,
        tenant,
        tmp_path,
    ) -> None:
        from surogates.tools.builtin.browser import _browser_screenshot_handler

        result = await _browser_screenshot_handler(
            {},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            workspace_path=str(tmp_path),
            _client_factory=lambda endpoint: FakeLargeScreenshotClient(),
        )
        body = json.loads(result)
        assert body["saved"] is True
        assert body["relative_path"].startswith("browser-screenshots/")
        assert body["path"] == str(tmp_path / body["relative_path"])
        assert (tmp_path / body["relative_path"]).read_bytes().startswith(b"\x89PNG")
        assert "base64" not in body
        assert "error" not in body

    async def test_oversized_png_is_saved_to_workspace_storage(
        self,
        tenant,
    ) -> None:
        from surogates.tools.builtin.browser import _browser_screenshot_handler

        session_id = uuid4()
        storage = FakeStorage()
        client = FakeSavePathScreenshotClient()

        result = await _browser_screenshot_handler(
            {},
            tenant=tenant,
            session_id=session_id,
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            workspace_path="/workspace",
            session_config={"storage_bucket": "agent-bucket"},
            storage=storage,
            _client_factory=lambda endpoint: client,
        )
        body = json.loads(result)

        assert body["saved"] is True
        assert body["relative_path"].startswith("browser-screenshots/")
        assert body["path"] == f"/workspace/{body['relative_path']}"
        assert "base64" not in body
        assert "error" not in body
        assert client.captured[0]["save_path"] == body["path"]
        assert storage.writes == [
            (
                "agent-bucket",
                f"{session_id}/{body['relative_path']}",
                b"\x89PNG\r\n\x1a\n" + (b"x" * 300_000),
            )
        ]

    async def test_unwritable_workspace_without_storage_fails_without_base64(
        self,
        tenant,
        tmp_path,
    ) -> None:
        from surogates.tools.builtin.browser import _browser_screenshot_handler

        workspace_file = tmp_path / "not-a-directory"
        workspace_file.write_text("not a directory")
        result = await _browser_screenshot_handler(
            {},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            workspace_path=str(workspace_file),
            _client_factory=lambda endpoint: FakeScreenshotClient(),
        )
        body = json.loads(result)
        assert body["error"] == "screenshot_save_failed"
        assert "base64" not in body
        assert "path" not in body

    async def test_annotate_saves_screenshot_and_returns_annotations(
        self,
        tenant,
        tmp_path,
    ) -> None:
        from surogates.tools.builtin.browser import _browser_screenshot_handler

        result = await _browser_screenshot_handler(
            {"annotate": True},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            workspace_path=str(tmp_path),
            _client_factory=lambda endpoint: FakeScreenshotClient(),
        )
        body = json.loads(result)
        assert body["saved"] is True
        assert "base64" not in body
        assert body["mime_type"] == "image/png"
        assert body["annotations"] == [
            {"ref": "@e1", "label": 1, "role": "button", "name": "Go"},
        ]

    async def test_requires_workspace_destination(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_screenshot_handler

        result = await _browser_screenshot_handler(
            {},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeScreenshotClient(),
        )
        body = json.loads(result)
        assert body["error"] == "workspace_unavailable"
        assert "base64" not in body


BROWSER_TOOL_NAMES = [
    "browser_navigate",
    "browser_get_state",
    "browser_screenshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_scroll",
    "browser_drag",
    "browser_wait",
    "browser_close",
]


class TestToolWiring:
    def test_router_locates_browser_tools_in_harness(self) -> None:
        from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

        for tool in BROWSER_TOOL_NAMES:
            assert TOOL_LOCATIONS[tool] == ToolLocation.HARNESS, tool

    def test_runtime_registers_browser_tools(self) -> None:
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.runtime import ToolRuntime

        registry = ToolRegistry()
        ToolRuntime(registry).register_builtins()

        for tool in BROWSER_TOOL_NAMES:
            assert registry.has(tool), tool

    def test_browser_scroll_schema_explains_direction(self) -> None:
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.runtime import ToolRuntime

        registry = ToolRegistry()
        ToolRuntime(registry).register_builtins()

        [schema] = registry.get_schemas(names={"browser_scroll"})
        function = schema["function"]
        delta_y = function["parameters"]["properties"]["delta_y"]
        assert "positive" in function["description"].lower()
        assert "scroll down" in function["description"].lower()
        assert "positive" in delta_y["description"].lower()
        assert "scroll down" in delta_y["description"].lower()

    def test_governance_url_arg_includes_browser_navigate(self) -> None:
        from surogates.governance.policy import _URL_ARGUMENT_MAP

        assert "url" in _URL_ARGUMENT_MAP["browser_navigate"]


class TestRouterDispatch:
    async def test_router_dispatches_browser_navigate(self, tenant) -> None:
        from surogates.governance.policy import GovernanceGate, PolicyDecision
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.router import ToolRouter
        from surogates.tools.runtime import ToolRuntime

        registry = ToolRegistry()
        ToolRuntime(registry).register_builtins()

        class AllowAll(GovernanceGate):
            def __init__(self) -> None:
                pass

            def check(self, *args: Any, **kwargs: Any) -> PolicyDecision:
                return PolicyDecision(allowed=True, reason="test", tool_name=str(args[0]))

        result = await ToolRouter(
            registry=registry,
            sandbox_pool=None,  # type: ignore[arg-type]
            governance=AllowAll(),
        ).execute(
            name="browser_navigate",
            arguments={"url": "https://example.com"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeClient(),
        )
        assert json.loads(result)["url"] == "https://example.com"
