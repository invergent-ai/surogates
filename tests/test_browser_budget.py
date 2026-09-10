"""Browser budget enforcement during provisioning and fleet lease requests."""

from __future__ import annotations

import json

import httpx
import pytest

from surogates.browser.base import (
    BrowserBudgetExhaustedError,
    BrowserSpec,
)
from surogates.browser.fleet import FleetBackend
from surogates.browser.pool import BrowserPool
from tests.test_browser_pool import FakeBackend, FakeRegistry

BILLING = {
    "owner_kind": "buyer",
    "owner_id": "ent-1",
    "reservation_id": "res-1",
    "balance_id": "bal-1",
}


async def test_pool_threads_billing_into_spec():
    backend = FakeBackend()
    specs: list[BrowserSpec] = []
    orig = backend.provision

    async def capture(spec, **kwargs):
        specs.append(spec)
        return await orig(spec, **kwargs)

    backend.provision = capture
    calls: list[dict] = []

    async def guard(**kwargs):
        calls.append(kwargs)
        return dict(BILLING)

    pool = BrowserPool(
        backend=backend, registry=FakeRegistry(), budget_guard=guard,
    )
    await pool.ensure("s1", "org", "user", BrowserSpec())
    assert calls == [{"session_id": "s1", "org_id": "org", "user_id": "user"}]
    assert specs[0].billing == BILLING

    # Reuse of a running pod never re-authorizes.
    await pool.ensure("s1", "org", "user", BrowserSpec())
    assert len(calls) == 1
    assert backend.provisions == 1


async def test_pool_budget_denial_blocks_provision():
    backend = FakeBackend()

    async def guard(**kwargs):
        raise BrowserBudgetExhaustedError("browser_minutes_exhausted")

    pool = BrowserPool(
        backend=backend, registry=FakeRegistry(), budget_guard=guard,
    )
    with pytest.raises(BrowserBudgetExhaustedError):
        await pool.ensure("s1", "org", "user", BrowserSpec())
    assert backend.provisions == 0


async def test_pool_none_billing_leaves_spec_clean():
    backend = FakeBackend()

    async def guard(**kwargs):
        return None

    pool = BrowserPool(
        backend=backend, registry=FakeRegistry(), budget_guard=guard,
    )
    spec = BrowserSpec()
    await pool.ensure("s1", "org", "user", spec)
    assert spec.billing is None


async def test_fleet_lease_body_includes_billing():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "lease_id": "L1",
            "browser_id": "b1",
            "endpoint": {
                "rest_url": "http://p:10001",
                "cdp_url": "ws://p:9222",
                "live_view_url": "ws://p:8080",
            },
        })

    backend = FleetBackend(
        endpoint="http://ops/api/browser-fleet",
        worker_token="tok",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    spec = BrowserSpec(billing=dict(BILLING))
    await backend.provision(spec, session_id="s1", org_id="p1", user_id="u1")
    assert captured["billing"] == BILLING


async def test_fleet_lease_body_omits_billing_when_absent():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "lease_id": "L1",
            "browser_id": "b1",
            "endpoint": {
                "rest_url": "http://p:10001",
                "cdp_url": "ws://p:9222",
                "live_view_url": "ws://p:8080",
            },
        })

    backend = FleetBackend(
        endpoint="http://ops/api/browser-fleet",
        worker_token="tok",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await backend.provision(
        BrowserSpec(), session_id="s1", org_id="p1", user_id="u1",
    )
    assert "billing" not in captured
