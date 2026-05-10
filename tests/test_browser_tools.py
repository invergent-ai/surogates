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
