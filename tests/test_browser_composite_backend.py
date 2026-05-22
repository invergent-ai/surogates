"""Tests for CompositeFallbackBackend — fleet → kubernetes/process fallback."""
from __future__ import annotations

import httpx
import pytest

from surogates.browser.base import BrowserEndpoint, BrowserSpec, BrowserStatus
from surogates.browser.composite import CompositeFallbackBackend
from surogates.browser.fleet import FleetAtCapacity


class _PrimaryAtCapacity:
    async def provision(self, spec, *, session_id, org_id, user_id):
        raise FleetAtCapacity({"reason": "fleet_at_capacity"})

    async def status(self, browser_id):
        return BrowserStatus.RUNNING

    async def destroy(self, browser_id):
        pass


class _PrimaryTransportError:
    async def provision(self, spec, *, session_id, org_id, user_id):
        raise httpx.ConnectError("ops unreachable")

    async def status(self, browser_id):
        raise httpx.ConnectError("ops unreachable")

    async def destroy(self, browser_id):
        raise httpx.ConnectError("ops unreachable")


class _PrimaryHappy:
    def __init__(self):
        self.destroyed: list[str] = []

    async def provision(self, spec, *, session_id, org_id, user_id):
        return "fleet-1", BrowserEndpoint("F-rest", "F-cdp", "F-live")

    async def status(self, browser_id):
        return BrowserStatus.RUNNING

    async def destroy(self, browser_id):
        self.destroyed.append(browser_id)


class _Fallback:
    def __init__(self):
        self.provision_calls: list[str] = []
        self.destroyed: list[str] = []

    async def provision(self, spec, *, session_id, org_id, user_id):
        self.provision_calls.append(session_id)
        return "fallback-1", BrowserEndpoint("K-rest", "K-cdp", "K-live")

    async def status(self, browser_id):
        return BrowserStatus.RUNNING

    async def destroy(self, browser_id):
        self.destroyed.append(browser_id)


@pytest.mark.asyncio
async def test_uses_fallback_on_fleet_at_capacity() -> None:
    fb = _Fallback()
    c = CompositeFallbackBackend(primary=_PrimaryAtCapacity(), fallback=fb)
    bid, ep = await c.provision(
        BrowserSpec(), session_id="S", org_id="O", user_id="U",
    )
    assert bid == "fallback-1"
    assert ep.rest_url == "K-rest"
    assert fb.provision_calls == ["S"]


@pytest.mark.asyncio
async def test_uses_fallback_on_transport_error() -> None:
    fb = _Fallback()
    c = CompositeFallbackBackend(primary=_PrimaryTransportError(), fallback=fb)
    bid, _ = await c.provision(
        BrowserSpec(), session_id="S", org_id="O", user_id="U",
    )
    assert bid == "fallback-1"


@pytest.mark.asyncio
async def test_routes_destroy_to_origin_backend() -> None:
    primary = _PrimaryHappy()
    fallback = _Fallback()
    c = CompositeFallbackBackend(primary=primary, fallback=fallback)

    pid, _ = await c.provision(BrowserSpec(), session_id="S1", org_id="O", user_id="U")
    await c.destroy(pid)
    assert primary.destroyed == ["fleet-1"]
    assert fallback.destroyed == []


@pytest.mark.asyncio
async def test_routes_destroy_to_fallback_when_provisioned_there() -> None:
    primary = _PrimaryAtCapacity()
    fallback = _Fallback()
    c = CompositeFallbackBackend(primary=primary, fallback=fallback)
    bid, _ = await c.provision(BrowserSpec(), session_id="S", org_id="O", user_id="U")
    await c.destroy(bid)
    assert fallback.destroyed == ["fallback-1"]


@pytest.mark.asyncio
async def test_unknown_browser_id_uses_primary_for_status() -> None:
    """A destroy after the routing table forgot the mapping (process
    restart) defaults to primary — the manager's release endpoint is
    idempotent on unknown lease ids."""
    primary = _PrimaryHappy()
    c = CompositeFallbackBackend(primary=primary, fallback=_Fallback())
    assert await c.status("never-seen") == BrowserStatus.RUNNING
