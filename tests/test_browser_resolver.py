"""Phase C browser resolver and event foundation tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from surogates.browser.base import BrowserEndpoint
from surogates.browser.registry import BrowserEntry
from surogates.browser.resolver import BrowserResolver, ResolvedBrowser
from surogates.session.events import EventType


def test_browser_control_event_types_exist() -> None:
    assert EventType.BROWSER_CONTROL_GRANTED.value == "browser.control_granted"
    assert EventType.BROWSER_CONTROL_RETURNED.value == "browser.control_returned"


def test_existing_browser_events_unchanged() -> None:
    assert EventType.BROWSER_PROVISIONED.value == "browser.provisioned"
    assert EventType.BROWSER_DESTROYED.value == "browser.destroyed"


class FakeRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, BrowserEntry] = {}

    async def get(self, session_id: str) -> BrowserEntry | None:
        return self.entries.get(session_id)


class FakeBackend:
    def __init__(self) -> None:
        self.found: dict[str, BrowserEntry] = {}
        self.calls: list[str] = []

    async def find_entry_by_session(self, session_id: str) -> BrowserEntry | None:
        self.calls.append(session_id)
        return self.found.get(session_id)


def _entry(
    session: str,
    *,
    org: str = "org-1",
    user: str = "user-1",
) -> BrowserEntry:
    return BrowserEntry(
        session_id=session,
        org_id=org,
        user_id=user,
        rest_url=f"http://browser-{session[:6]}.svc:10001",
        cdp_url=f"ws://browser-{session[:6]}.svc:9222",
        live_view_url=f"ws://browser-{session[:6]}.svc:443",
        provisioned_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
class TestResolveFromRegistry:
    async def test_hits_registry(self) -> None:
        reg = FakeRegistry()
        reg.entries["sess-1"] = _entry("sess-1")
        backend = FakeBackend()
        resolver = BrowserResolver(registry=reg, backend=backend)  # type: ignore[arg-type]

        result = await resolver.resolve("sess-1", expected_org_id="org-1")

        assert isinstance(result, ResolvedBrowser)
        assert result.session_id == "sess-1"
        assert result.endpoint.rest_url == "http://browser-sess-1.svc:10001"
        assert result.org_id == "org-1"
        assert result.user_id == "user-1"
        assert result.source == "registry"
        assert backend.calls == []

    async def test_tenant_mismatch_returns_none(self) -> None:
        reg = FakeRegistry()
        reg.entries["sess-1"] = _entry("sess-1", org="org-OWN")
        resolver = BrowserResolver(registry=reg, backend=FakeBackend())  # type: ignore[arg-type]

        assert await resolver.resolve("sess-1", expected_org_id="org-OTHER") is None


@pytest.mark.asyncio
class TestFallbackToBackend:
    async def test_uses_backend_when_registry_misses(self) -> None:
        backend = FakeBackend()
        backend.found["sess-1"] = BrowserEntry(
            session_id="sess-1",
            org_id="org-1",
            user_id="user-1",
            rest_url="http://browser-x.svc:10001",
            cdp_url="ws://browser-x.svc:9222",
            live_view_url="ws://browser-x.svc:443",
            provisioned_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        resolver = BrowserResolver(registry=FakeRegistry(), backend=backend)  # type: ignore[arg-type]

        result = await resolver.resolve("sess-1", expected_org_id="org-1")

        assert result is not None
        assert result.endpoint.rest_url == "http://browser-x.svc:10001"
        assert result.org_id == "org-1"
        assert result.source == "k8s_fallback"

    async def test_backend_tenant_mismatch_returns_none(self) -> None:
        backend = FakeBackend()
        backend.found["sess-1"] = _entry("sess-1", org="org-OTHER")
        resolver = BrowserResolver(registry=FakeRegistry(), backend=backend)  # type: ignore[arg-type]

        assert await resolver.resolve("sess-1", expected_org_id="org-1") is None

    async def test_returns_none_when_neither_path_finds(self) -> None:
        resolver = BrowserResolver(
            registry=FakeRegistry(),
            backend=FakeBackend(),  # type: ignore[arg-type]
        )

        assert await resolver.resolve("never", expected_org_id="org-1") is None


@pytest.mark.asyncio
class TestNoBackend:
    async def test_no_backend_means_registry_only(self) -> None:
        reg = FakeRegistry()
        reg.entries["sess-1"] = _entry("sess-1")
        resolver = BrowserResolver(registry=reg, backend=None)

        result = await resolver.resolve("sess-1", expected_org_id="org-1")

        assert result is not None


@pytest.mark.asyncio
class TestEndpointValue:
    async def test_resolved_endpoint_is_value_type(self) -> None:
        reg = FakeRegistry()
        reg.entries["sess-1"] = _entry("sess-1")
        resolver = BrowserResolver(registry=reg, backend=None)

        result = await resolver.resolve("sess-1", expected_org_id="org-1")

        assert result is not None
        assert isinstance(result.endpoint, BrowserEndpoint)
