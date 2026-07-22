"""Commerce enforcement on the authenticated web channel.

The web app derives the buyer identity from the signed-in user's
Firebase uid (``external_id``) at message time and feeds the same
``authorize_commerce_turn`` gate the website widget uses — one
pipeline, two channels.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from surogates.api.routes._commerce_turn import authorize_commerce_turn
from surogates.runtime.platform_client import CommercePaymentRequiredError


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


def _request(*, runtime_payload=None, platform_client=None, store=None):
    state = SimpleNamespace()
    if runtime_payload is not None:
        state.runtime_config_cache = _FakeCache({"a-1": runtime_payload})
    if platform_client is not None:
        state.platform_client = platform_client
    if store is not None:
        state.session_store = store
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _session():
    return SimpleNamespace(id=uuid.uuid4(), agent_id="a-1", config={})


_PAID = {
    "commerce_mode": "subscription_required",
    "commerce_buy_url": "https://studio.example/buy/a-1",
}

_BUYER = {
    "firebase_uid": "uid-web-1",
    "email": "web@example.com",
    "name": "Web User",
}


@pytest.mark.asyncio
async def test_explicit_buyer_reserves_and_pins_receipt():
    store = _FakeStore()
    client = _FakePlatformClient(
        receipt={
            "entitlement_id": "ent-1",
            "reserved_tokens": 42,
            "reservation_id": "res-1",
        },
    )
    request = _request(
        runtime_payload=_PAID, platform_client=client, store=store,
    )
    session = _session()
    await authorize_commerce_turn(request, session, "hello", buyer=_BUYER)
    assert client.calls and client.calls[0]["firebase_uid"] == "uid-web-1"
    assert store.updates
    _, key, value = store.updates[0]
    assert key == "commerce_reservations"
    assert value["entitlement_id"] == "ent-1"
    assert value["reserved_tokens"] == 42


@pytest.mark.asyncio
async def test_explicit_buyer_out_of_tokens_402s_with_buy_url():
    client = _FakePlatformClient(
        error=CommercePaymentRequiredError("payment_required"),
    )
    request = _request(runtime_payload=_PAID, platform_client=client)
    with pytest.raises(HTTPException) as err:
        await authorize_commerce_turn(
            request, _session(), "hello", buyer=_BUYER,
        )
    assert err.value.status_code == 402
    assert err.value.detail["buy_url"] == "https://studio.example/buy/a-1"


@pytest.mark.asyncio
async def test_free_mode_ignores_explicit_buyer():
    client = _FakePlatformClient()
    request = _request(
        runtime_payload={"commerce_mode": "free"}, platform_client=client,
    )
    await authorize_commerce_turn(
        request, _session(), "hello", buyer=_BUYER,
    )
    assert client.calls == []


@pytest.mark.asyncio
async def test_explicit_buyer_overrides_session_config_binding():
    """The web channel passes the tenant user's identity even when the
    session config carries no widget-era binding."""
    store = _FakeStore()
    client = _FakePlatformClient(
        receipt={
            "entitlement_id": "ent-2",
            "reserved_tokens": 7,
            "reservation_id": "res-2",
        },
    )
    request = _request(
        runtime_payload=_PAID, platform_client=client, store=store,
    )
    session = _session()
    session.config = {}
    await authorize_commerce_turn(request, session, "hi", buyer=_BUYER)
    assert client.calls[0]["email"] == "web@example.com"
