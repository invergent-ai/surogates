"""Per-buyer browser-minutes gate: platform client, worker guard,
pool integration, and the billing block's ride to both backends."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from surogates.browser.base import (
    BrowserBudgetExhaustedError,
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
    browser_budget_exhausted_result,
)
from surogates.browser.fleet import FleetBackend
from surogates.browser.pool import BrowserPool
from surogates.orchestrator.worker import _build_browser_budget_guard
from surogates.runtime.platform_client import (
    BrowserMinutesExhaustedError,
    PlatformClient,
)

BILLING = {
    "owner_kind": "buyer",
    "owner_id": "ent-1",
    "reservation_id": "res-1",
    "balance_id": "bal-1",
}


# ── platform client ─────────────────────────────────────────────────


def _client(handler):
    transport = httpx.MockTransport(handler)
    return PlatformClient(base_url="http://ops", token="t", transport=transport)


async def test_browser_authorize_returns_receipt():
    def handler(request):
        assert request.url.path == "/api/agents/agents/a1/browser/authorize"
        body = json.loads(request.content)
        assert body == {
            "session_id": "s1",
            "firebase_uid": "fb1",
            "end_user_id": "eu1",
        }
        return httpx.Response(200, json={
            "metered": True,
            "reservation_id": "res-1",
            "balance_id": "bal-1",
            "reserved_minutes": 10,
            "owner_kind": "buyer",
            "owner_id": "ent-1",
        })

    pc = _client(handler)
    out = await pc.browser_authorize(
        "a1", session_id="s1", firebase_uid="fb1", end_user_id="eu1",
    )
    assert out["metered"] is True
    assert out["reservation_id"] == "res-1"


async def test_browser_authorize_402_raises_typed_error():
    def handler(request):
        return httpx.Response(
            402, json={"detail": "browser_minutes_exhausted"},
        )

    pc = _client(handler)
    with pytest.raises(BrowserMinutesExhaustedError) as err:
        await pc.browser_authorize("a1", end_user_id="eu1")
    assert err.value.detail == "browser_minutes_exhausted"


# ── worker guard ────────────────────────────────────────────────────


class _FakeRow:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _FakeDb:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return _FakeRow(self._row)


def _session_factory(row):
    return lambda: _FakeDb(row)


class _FakeCache:
    def __init__(self, payload):
        self.payload = payload
        self.requested: list[str] = []

    async def get(self, agent_id: str):
        self.requested.append(agent_id)
        if self.payload is None:
            raise LookupError(agent_id)
        return self.payload


class _FakePlatform:
    def __init__(self, receipt=None, error: Exception | None = None):
        self.receipt = receipt or {"metered": True, **BILLING}
        self.error = error
        self.calls: list[dict] = []

    async def browser_authorize(self, agent_id, **kwargs):
        self.calls.append({"agent_id": agent_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.receipt


_SID = str(uuid.uuid4())


def _row(
    *,
    channel: str = "web",
    user_id: Any = None,
    service_account_id: Any = None,
    config: dict | None = None,
):
    return ("agent-1", channel, user_id, service_account_id, config or {})


async def test_guard_skips_unmetered_agents():
    platform = _FakePlatform()
    guard = _build_browser_budget_guard(
        _session_factory(_row()),
        platform,
        _FakeCache({"browser_minutes_metered": False}),
    )
    assert await guard(session_id=_SID, org_id="o", user_id="u") is None
    assert platform.calls == []


async def test_guard_skips_browser_setup_sessions():
    platform = _FakePlatform()
    guard = _build_browser_budget_guard(
        _session_factory(_row(channel="browser_setup")),
        platform,
        _FakeCache({"browser_minutes_metered": True}),
    )
    assert await guard(session_id=_SID, org_id="o", user_id="u") is None
    assert platform.calls == []


async def test_guard_returns_billing_block_for_metered_sender():
    platform = _FakePlatform()
    guard = _build_browser_budget_guard(
        _session_factory(_row(config={"embed_end_user_id": "eu-7"})),
        platform,
        _FakeCache({"browser_minutes_metered": True}),
    )
    billing = await guard(session_id=_SID, org_id="o", user_id="")
    assert billing == BILLING
    assert platform.calls[0]["end_user_id"] == "eu-7"
    assert platform.calls[0]["agent_id"] == "agent-1"


async def test_guard_prefers_commerce_buyer_identity():
    platform = _FakePlatform()
    guard = _build_browser_budget_guard(
        _session_factory(
            _row(config={"commerce_buyer": {"firebase_uid": "fb-9"}}),
        ),
        platform,
        _FakeCache({"browser_minutes_metered": True}),
    )
    await guard(session_id=_SID, org_id="o", user_id="u-1")
    call = platform.calls[0]
    assert call["firebase_uid"] == "fb-9"
    assert call["end_user_id"] == "u-1"


async def test_guard_falls_back_to_session_row_user():
    platform = _FakePlatform()
    guard = _build_browser_budget_guard(
        _session_factory(_row(user_id=uuid.UUID(int=7))),
        platform,
        _FakeCache({"browser_minutes_metered": True}),
    )
    await guard(session_id=_SID, org_id="o", user_id="")
    assert platform.calls[0]["end_user_id"] == str(uuid.UUID(int=7))


async def test_guard_anonymous_sender_gets_buy_prompt():
    platform = _FakePlatform()
    guard = _build_browser_budget_guard(
        _session_factory(_row()),
        platform,
        _FakeCache({
            "browser_minutes_metered": True,
            "commerce_buy_url": "https://buy.example/x",
        }),
    )
    with pytest.raises(BrowserBudgetExhaustedError) as err:
        await guard(session_id=_SID, org_id="o", user_id="")
    assert err.value.buy_url == "https://buy.example/x"
    assert platform.calls == []


async def test_guard_maps_402_to_budget_exhausted():
    platform = _FakePlatform(
        error=BrowserMinutesExhaustedError("browser_minutes_exhausted"),
    )
    guard = _build_browser_budget_guard(
        _session_factory(_row(config={"embed_end_user_id": "eu-7"})),
        platform,
        _FakeCache({
            "browser_minutes_metered": True,
            "commerce_buy_url": "https://buy.example/x",
        }),
    )
    with pytest.raises(BrowserBudgetExhaustedError) as err:
        await guard(session_id=_SID, org_id="o", user_id="")
    assert err.value.buy_url == "https://buy.example/x"


async def test_guard_fails_closed_when_ops_unreachable():
    platform = _FakePlatform(error=RuntimeError("connection refused"))
    guard = _build_browser_budget_guard(
        _session_factory(_row(config={"embed_end_user_id": "eu-7"})),
        platform,
        _FakeCache({"browser_minutes_metered": True}),
    )
    with pytest.raises(BrowserUnavailableError):
        await guard(session_id=_SID, org_id="o", user_id="")


async def test_guard_unmetered_receipt_returns_none():
    platform = _FakePlatform(receipt={"metered": False})
    guard = _build_browser_budget_guard(
        _session_factory(_row(config={"embed_end_user_id": "eu-7"})),
        platform,
        _FakeCache({"browser_minutes_metered": True}),
    )
    assert await guard(session_id=_SID, org_id="o", user_id="") is None


# ── pool integration ────────────────────────────────────────────────


from tests.test_browser_pool import FakeBackend, FakeRegistry  # noqa: E402


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


# ── backends carry the block ────────────────────────────────────────


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


def test_k8s_manifest_stamps_billing_labels():
    from surogates.browser.kubernetes import K8sBrowserBackend

    backend = K8sBrowserBackend(namespace="test-ns")
    pod = backend._build_pod_manifest(
        browser_id="bid",
        pod_name="browser-x",
        session_id="s1",
        org_id="p1",
        user_id="u1",
        spec=BrowserSpec(billing=dict(BILLING)),
    )
    labels = pod.metadata.labels
    assert labels["surogates.ai/minutes-owner-kind"] == "buyer"
    assert labels["surogates.ai/minutes-owner-id"] == "ent-1"
    assert labels["surogates.ai/minutes-reservation-id"] == "res-1"
    assert labels["surogates.ai/minutes-balance-id"] == "bal-1"


def test_k8s_manifest_omits_billing_labels_when_absent():
    from surogates.browser.kubernetes import K8sBrowserBackend

    backend = K8sBrowserBackend(namespace="test-ns")
    pod = backend._build_pod_manifest(
        browser_id="bid",
        pod_name="browser-x",
        session_id="s1",
        org_id="p1",
        user_id="u1",
        spec=BrowserSpec(),
    )
    assert not any(
        key.startswith("surogates.ai/minutes-")
        for key in pod.metadata.labels
    )


# ── tool-layer rendering ────────────────────────────────────────────


def test_budget_exhausted_result_carries_buy_url():
    body = json.loads(browser_budget_exhausted_result(
        "browser_minutes_exhausted", buy_url="https://buy.example/x",
    ))
    assert body["error"] == "browser_minutes_exhausted"
    assert body["buy_url"] == "https://buy.example/x"
    assert "buy" in body["guidance"]


async def test_tool_preflight_renders_buy_prompt():
    from surogates.tools.builtin.browser import _resolve_session_browser

    class _Pool:
        browser_profile_store = None

        async def ensure(self, **kwargs):
            raise BrowserBudgetExhaustedError(
                "browser_minutes_exhausted",
                buy_url="https://buy.example/x",
            )

    out = await _resolve_session_browser(
        tenant=None,
        session_id="s1",
        browser_pool=_Pool(),
        browser_control=None,
    )
    assert isinstance(out, str)
    body = json.loads(out)
    assert body["error"] == "browser_minutes_exhausted"
    assert body["buy_url"] == "https://buy.example/x"


async def test_guard_skips_service_account_sessions():
    """Operator-plane sessions (ops chat, api, scheduled runs) run
    under a service account and are never metered — mirroring the
    token plane's end-user-only scope."""
    platform = _FakePlatform()
    guard = _build_browser_budget_guard(
        _session_factory(_row(service_account_id=uuid.UUID(int=3))),
        platform,
        _FakeCache({"browser_minutes_metered": True}),
    )
    assert await guard(session_id=_SID, org_id="o", user_id="") is None
    assert platform.calls == []


async def test_guard_wraps_cache_failures_as_unavailable():
    """A control-plane blip during the metered check degrades to the
    structured browser_unavailable result, never a raw traceback."""
    import httpx as _httpx

    class _FlakyCache:
        async def get(self, agent_id):
            raise _httpx.ConnectError("boom")

    platform = _FakePlatform()
    guard = _build_browser_budget_guard(
        _session_factory(_row(config={"embed_end_user_id": "eu-7"})),
        platform,
        _FlakyCache(),
    )
    with pytest.raises(BrowserUnavailableError):
        await guard(session_id=_SID, org_id="o", user_id="")
    assert platform.calls == []


def test_k8s_billing_labels_are_sanitized():
    from surogates.browser.kubernetes import K8sBrowserBackend

    backend = K8sBrowserBackend(namespace="test-ns")
    pod = backend._build_pod_manifest(
        browser_id="bid",
        pod_name="browser-x",
        session_id="s1",
        org_id="p1",
        user_id="u1",
        spec=BrowserSpec(billing={
            "owner_kind": "end_user",
            "owner_id": "visitor@example.com",
            "reservation_id": "res-1",
            "balance_id": "bal-1",
        }),
    )
    assert pod.metadata.labels["surogates.ai/minutes-owner-id"] == (
        "visitor-example.com"
    )
