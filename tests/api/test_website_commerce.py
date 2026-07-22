"""Commerce enforcement on the website channel.

Covers the two route-level hooks:

- ``_resolve_commerce_buyer`` — bootstrap-time Firebase verification
  that pins the buyer identity on the session config.
- ``authorize_commerce_turn`` — message-accept gate: free agents pass
  through untouched; monetized agents require a bound buyer and an
  ops-side reservation, with structured 402 details the widget maps to
  its paywall.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from surogates.api.routes import website
from surogates.runtime.platform_client import (
    CommercePaymentRequiredError,
    PlatformAuthError,
)


class _FakeCache:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, key):
        if key not in self._rows:
            raise LookupError(key)
        return self._rows[key]


class _FakeStore:
    def __init__(self):
        self.updates: list[tuple] = []

    async def append_session_config_list(self, session_id, key, value):
        self.updates.append((session_id, key, value))


class _FakePlatformClient:
    def __init__(self, *, receipt=None, error=None):
        self.receipt = receipt
        self.error = error
        self.calls: list[dict] = []

    async def commerce_authorize(self, agent_id, **kwargs):
        self.calls.append({"agent_id": agent_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.receipt


def _request(
    *,
    runtime_payload=None,
    platform_client=None,
    firebase_rows=None,
    store=None,
):
    state = SimpleNamespace()
    if runtime_payload is not None:
        state.runtime_config_cache = _FakeCache({"a-1": runtime_payload})
    if platform_client is not None:
        state.platform_client = platform_client
    if firebase_rows is not None:
        state.firebase_config_cache = _FakeCache(firebase_rows)
    if store is not None:
        state.session_store = store
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _session(config=None):
    return SimpleNamespace(
        id=uuid.uuid4(), agent_id="a-1", config=config or {},
    )


_PAID_PAYLOAD = {
    "project_id": "p-1",
    "commerce_mode": "token_packs_only",
    "commerce_buy_url": "https://studio.example/buy/a-1",
}


# ── authorize_commerce_turn ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_free_mode_passes_without_touching_the_platform():
    client = _FakePlatformClient()
    request = _request(
        runtime_payload={"commerce_mode": "free"}, platform_client=client,
    )
    await website.authorize_commerce_turn(request, _session(), "hello")
    assert client.calls == []


@pytest.mark.asyncio
async def test_missing_runtime_cache_passes_through():
    request = _request()
    await website.authorize_commerce_turn(request, _session(), "hello")


@pytest.mark.asyncio
async def test_paid_without_buyer_402_sign_in_required():
    request = _request(runtime_payload=_PAID_PAYLOAD)
    with pytest.raises(HTTPException) as exc_info:
        await website.authorize_commerce_turn(request, _session(), "hello")
    assert exc_info.value.status_code == 402
    assert exc_info.value.detail == {
        "code": "sign_in_required",
        "buy_url": "https://studio.example/buy/a-1",
    }


@pytest.mark.asyncio
async def test_paid_with_buyer_reserves_and_pins_receipt():
    client = _FakePlatformClient(
        receipt={
            "entitlement_id": "ent-1",
            "reserved_tokens": 41,
            "reservation_id": "res-1",
        },
    )
    store = _FakeStore()
    request = _request(
        runtime_payload=_PAID_PAYLOAD, platform_client=client, store=store,
    )
    session = _session(
        config={
            "commerce_buyer": {
                "firebase_uid": "fb-1",
                "email": "b@example.com",
                "name": "B",
            },
        },
    )
    await website.authorize_commerce_turn(request, session, "x" * 100)

    assert client.calls == [
        {
            "agent_id": "a-1",
            "firebase_uid": "fb-1",
            "estimated_tokens": (100 + 16) // 4,
            "email": "b@example.com",
            "name": "B",
        },
    ]
    assert store.updates == [
        (
            session.id,
            "commerce_reservations",
            {
                "entitlement_id": "ent-1",
                "reserved_tokens": 41,
                "reservation_id": "res-1",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_paid_402_from_ops_maps_to_structured_detail():
    client = _FakePlatformClient(
        error=CommercePaymentRequiredError("insufficient_tokens"),
    )
    request = _request(
        runtime_payload=_PAID_PAYLOAD, platform_client=client,
    )
    session = _session(config={"commerce_buyer": {"firebase_uid": "fb-1"}})
    with pytest.raises(HTTPException) as exc_info:
        await website.authorize_commerce_turn(request, session, "hello")
    assert exc_info.value.status_code == 402
    assert exc_info.value.detail == {
        "code": "insufficient_tokens",
        "buy_url": "https://studio.example/buy/a-1",
    }


@pytest.mark.asyncio
async def test_paid_platform_failure_fails_closed_503():
    client = _FakePlatformClient(error=PlatformAuthError("bad token"))
    request = _request(
        runtime_payload=_PAID_PAYLOAD, platform_client=client,
    )
    session = _session(config={"commerce_buyer": {"firebase_uid": "fb-1"}})
    with pytest.raises(HTTPException) as exc_info:
        await website.authorize_commerce_turn(request, session, "hello")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_paid_without_platform_client_fails_closed_503():
    request = _request(runtime_payload=_PAID_PAYLOAD)
    session = _session(config={"commerce_buyer": {"firebase_uid": "fb-1"}})
    with pytest.raises(HTTPException) as exc_info:
        await website.authorize_commerce_turn(request, session, "hello")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_free_receipt_is_not_pinned():
    client = _FakePlatformClient(
        receipt={
            "entitlement_id": "",
            "reserved_tokens": 0,
            "reservation_id": "",
        },
    )
    store = _FakeStore()
    request = _request(
        runtime_payload=_PAID_PAYLOAD, platform_client=client, store=store,
    )
    session = _session(config={"commerce_buyer": {"firebase_uid": "fb-1"}})
    await website.authorize_commerce_turn(request, session, "hello")
    assert store.updates == []


# ── _resolve_commerce_buyer ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_buyer_verifies_and_returns_identity(monkeypatch):
    async def fake_verify(token, project):
        assert token == "id-token"
        assert project == "fb-project"
        return {"sub": "fb-1", "email": "b@example.com", "name": "B"}

    monkeypatch.setattr(website, "verify_firebase_id_token", fake_verify)
    request = _request(
        runtime_payload={"project_id": "p-1"},
        firebase_rows={
            "p-1": SimpleNamespace(firebase_project_id="fb-project"),
        },
    )
    buyer = await website._resolve_commerce_buyer(request, "a-1", "id-token")
    assert buyer == {
        "firebase_uid": "fb-1",
        "email": "b@example.com",
        "name": "B",
    }


@pytest.mark.asyncio
async def test_resolve_buyer_normalizes_claims(monkeypatch):
    async def fake_verify(token, project):
        return {"sub": "fb-2", "email": "  MiXeD@Example.COM "}

    monkeypatch.setattr(website, "verify_firebase_id_token", fake_verify)
    request = _request(
        runtime_payload={"project_id": "p-1"},
        firebase_rows={
            "p-1": SimpleNamespace(firebase_project_id="fb-project"),
        },
    )
    buyer = await website._resolve_commerce_buyer(request, "a-1", "id-token")
    assert buyer == {
        "firebase_uid": "fb-2",
        "email": "mixed@example.com",
        "name": "mixed",
    }


@pytest.mark.asyncio
async def test_resolve_buyer_invalid_token_degrades_to_anonymous(
    monkeypatch,
):
    from surogates.tenant.auth.firebase import FirebaseTokenError

    async def fake_verify(token, project):
        raise FirebaseTokenError("expired")

    monkeypatch.setattr(website, "verify_firebase_id_token", fake_verify)
    request = _request(
        runtime_payload={"project_id": "p-1"},
        firebase_rows={
            "p-1": SimpleNamespace(firebase_project_id="fb-project"),
        },
    )
    assert (
        await website._resolve_commerce_buyer(request, "a-1", "id-token")
    ) is None


@pytest.mark.asyncio
async def test_resolve_buyer_unconfigured_firebase_degrades():
    request = _request(
        runtime_payload={"project_id": "p-1"}, firebase_rows={},
    )
    assert (
        await website._resolve_commerce_buyer(request, "a-1", "id-token")
    ) is None


@pytest.mark.asyncio
async def test_resolve_buyer_missing_subject_degrades(monkeypatch):
    async def fake_verify(token, project):
        return {"email": "b@example.com"}

    monkeypatch.setattr(website, "verify_firebase_id_token", fake_verify)
    request = _request(
        runtime_payload={"project_id": "p-1"},
        firebase_rows={
            "p-1": SimpleNamespace(firebase_project_id="fb-project"),
        },
    )
    assert (
        await website._resolve_commerce_buyer(request, "a-1", "id-token")
    ) is None
