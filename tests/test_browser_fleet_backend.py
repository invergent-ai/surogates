"""Tests for FleetBackend — worker → surogate-ops lease client.

The backend turns ``BrowserBackend.provision`` into a single
``POST /api/browser-fleet/lease`` call against surogate-ops, and
``destroy`` into the mirror ``POST /release``. ``status`` polls the
per-pod status route. No K8s interactions here.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from surogates.browser.base import BrowserSpec, BrowserStatus
from surogates.browser.fleet import FleetAtCapacity, FleetBackend


@pytest.mark.asyncio
@respx.mock
async def test_provision_calls_lease_and_returns_endpoint() -> None:
    respx.post("http://ops/api/browser-fleet/lease").mock(
        return_value=httpx.Response(200, json={
            "lease_id": "L1",
            "browser_id": "b1",
            "endpoint": {
                "rest_url": "http://10/9333",
                "cdp_url": "http://10/9222",
                "live_view_url": "http://10/7900",
            },
        }),
    )
    async with httpx.AsyncClient() as http:
        b = FleetBackend(
            endpoint="http://ops/api/browser-fleet",
            worker_token="tok",
            http=http,
        )
        spec = BrowserSpec(workspace_source_ref=None, env={})
        browser_id, endpoint = await b.provision(
            spec, session_id="S", org_id="O", user_id="U",
        )
    assert browser_id == "b1"
    assert endpoint.rest_url == "http://10/9333"


@pytest.mark.asyncio
@respx.mock
async def test_provision_sends_bearer_token() -> None:
    route = respx.post("http://ops/api/browser-fleet/lease").mock(
        return_value=httpx.Response(200, json={
            "lease_id": "L", "browser_id": "b",
            "endpoint": {"rest_url": "x", "cdp_url": "y", "live_view_url": "z"},
        }),
    )
    async with httpx.AsyncClient() as http:
        b = FleetBackend(
            endpoint="http://ops/api/browser-fleet",
            worker_token="my-token", http=http,
        )
        await b.provision(BrowserSpec(), session_id="S", org_id="O", user_id="U")
    assert route.calls[0].request.headers["authorization"] == "Bearer my-token"


@pytest.mark.asyncio
@respx.mock
async def test_provision_forwards_session_creds() -> None:
    import json as _json

    route = respx.post("http://ops/api/browser-fleet/lease").mock(
        return_value=httpx.Response(200, json={
            "lease_id": "L", "browser_id": "b",
            "endpoint": {"rest_url": "x", "cdp_url": "y", "live_view_url": "z"},
        }),
    )

    class _Storage:
        access_key = "AK"
        secret_key = "SK"
        region = "auto"
        endpoint = "https://r2.x/"

    async with httpx.AsyncClient() as http:
        b = FleetBackend(
            endpoint="http://ops/api/browser-fleet",
            worker_token="tok", http=http,
            storage_settings=_Storage(),
        )
        spec = BrowserSpec(
            workspace_source_ref="s3://b/sess/", env={"FOO": "BAR"},
        )
        await b.provision(spec, session_id="S", org_id="O", user_id="U")
    sent = _json.loads(route.calls[0].request.read())
    assert sent["workspace_source_ref"] == "s3://b/sess/"
    assert sent["s3_creds"]["access_key"] == "AK"
    assert sent["s3_creds"]["secret_key"] == "SK"
    assert sent["s3_creds"]["endpoint"] == "https://r2.x/"
    assert sent["env"] == {"FOO": "BAR"}


@pytest.mark.asyncio
@respx.mock
async def test_provision_without_workspace_omits_s3_creds() -> None:
    import json as _json

    route = respx.post("http://ops/api/browser-fleet/lease").mock(
        return_value=httpx.Response(200, json={
            "lease_id": "L", "browser_id": "b",
            "endpoint": {"rest_url": "x", "cdp_url": "y", "live_view_url": "z"},
        }),
    )
    async with httpx.AsyncClient() as http:
        b = FleetBackend(
            endpoint="http://ops/api/browser-fleet",
            worker_token="tok", http=http,
        )
        await b.provision(BrowserSpec(), session_id="S", org_id="O", user_id="U")
    sent = _json.loads(route.calls[0].request.read())
    assert sent["s3_creds"] is None


@pytest.mark.asyncio
@respx.mock
async def test_provision_raises_fleet_at_capacity_on_503() -> None:
    respx.post("http://ops/api/browser-fleet/lease").mock(
        return_value=httpx.Response(
            503,
            json={"reason": "fleet_at_capacity", "retry_after_ms": 1000},
        ),
    )
    async with httpx.AsyncClient() as http:
        b = FleetBackend(
            endpoint="http://ops/api/browser-fleet",
            worker_token="tok", http=http,
        )
        with pytest.raises(FleetAtCapacity) as exc:
            await b.provision(
                BrowserSpec(), session_id="S", org_id="O", user_id="U",
            )
        assert exc.value.payload["reason"] == "fleet_at_capacity"


@pytest.mark.asyncio
@respx.mock
async def test_destroy_calls_release_with_lease_id() -> None:
    respx.post("http://ops/api/browser-fleet/lease").mock(
        return_value=httpx.Response(200, json={
            "lease_id": "L1", "browser_id": "b1",
            "endpoint": {"rest_url": "x", "cdp_url": "y", "live_view_url": "z"},
        }),
    )
    release_route = respx.post("http://ops/api/browser-fleet/release").mock(
        return_value=httpx.Response(204),
    )
    async with httpx.AsyncClient() as http:
        b = FleetBackend(
            endpoint="http://ops/api/browser-fleet",
            worker_token="tok", http=http,
        )
        await b.provision(BrowserSpec(), session_id="S", org_id="O", user_id="U")
        await b.destroy("b1")
    assert release_route.called
    import json as _json

    sent = _json.loads(release_route.calls[0].request.read())
    assert sent == {"lease_id": "L1", "browser_id": "b1"}


@pytest.mark.asyncio
async def test_destroy_silently_skips_unknown_browser_id() -> None:
    """Worker may call destroy for a browser that was never leased
    through this backend (e.g., a fallback path leaked through)."""
    async with httpx.AsyncClient() as http:
        b = FleetBackend(
            endpoint="http://ops/api/browser-fleet",
            worker_token="tok", http=http,
        )
        # Must not raise; no HTTP call expected because we don't have a
        # lease id to release.
        await b.destroy("never-seen")


@pytest.mark.asyncio
@respx.mock
async def test_status_maps_running_pending_failed_terminated() -> None:
    for raw, expected in [
        ("running", BrowserStatus.RUNNING),
        ("pending", BrowserStatus.PENDING),
        ("failed", BrowserStatus.FAILED),
        ("terminated", BrowserStatus.TERMINATED),
    ]:
        respx.get("http://ops/api/browser-fleet/pod/b1/status").mock(
            return_value=httpx.Response(200, json={"status": raw}),
        )
        async with httpx.AsyncClient() as http:
            b = FleetBackend(
                endpoint="http://ops/api/browser-fleet",
                worker_token="tok", http=http,
            )
            assert await b.status("b1") == expected
        respx.reset()
