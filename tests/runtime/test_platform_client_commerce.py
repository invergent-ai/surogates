"""PlatformClient commerce methods (authorize / debit).

MockTransport-level tests: the wire shapes must match the ops
runtime-plane endpoints exactly, 402 must surface as the typed
paywall error (not a generic HTTPStatusError), and 401 keeps the
established ``PlatformAuthError`` semantics.
"""

from __future__ import annotations

import json

import httpx
import pytest

from surogates.runtime.platform_client import (
    CommercePaymentRequiredError,
    PlatformAuthError,
    PlatformClient,
)


def _client(handler) -> PlatformClient:
    return PlatformClient(
        base_url="http://ops.test",
        token="tok",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_authorize_posts_body_and_returns_receipt():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "entitlement_id": "ent-1",
                "reserved_tokens": 500,
                "reservation_id": "res-1",
            },
        )

    receipt = await _client(handler).commerce_authorize(
        "a-1",
        firebase_uid="fb-1",
        estimated_tokens=500,
        email="b@example.com",
    )
    assert seen["url"].endswith("/api/agents/agents/a-1/commerce/authorize")
    assert seen["body"] == {
        "firebase_uid": "fb-1",
        "estimated_tokens": 500,
        "email": "b@example.com",
    }
    assert receipt["entitlement_id"] == "ent-1"
    assert receipt["reservation_id"] == "res-1"


@pytest.mark.asyncio
async def test_authorize_402_raises_typed_paywall_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "insufficient_tokens"})

    with pytest.raises(CommercePaymentRequiredError) as exc_info:
        await _client(handler).commerce_authorize(
            "a-1", firebase_uid="fb-1", estimated_tokens=100,
        )
    assert exc_info.value.detail == "insufficient_tokens"


@pytest.mark.asyncio
async def test_authorize_402_without_json_body_still_typed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="nope")

    with pytest.raises(CommercePaymentRequiredError) as exc_info:
        await _client(handler).commerce_authorize(
            "a-1", firebase_uid="fb-1", estimated_tokens=100,
        )
    assert exc_info.value.detail == "payment_required"


@pytest.mark.asyncio
async def test_authorize_401_raises_platform_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(PlatformAuthError):
        await _client(handler).commerce_authorize(
            "a-1", firebase_uid="fb-1", estimated_tokens=100,
        )


@pytest.mark.asyncio
async def test_debit_posts_settlement_body():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"debited_tokens": 240})

    result = await _client(handler).commerce_debit(
        "a-1",
        entitlement_id="ent-1",
        reserved_tokens=500,
        actual_tokens=240,
        reservation_id="res-1",
    )
    assert seen["url"].endswith("/api/agents/agents/a-1/commerce/debit")
    assert seen["body"] == {
        "entitlement_id": "ent-1",
        "reserved_tokens": 500,
        "actual_tokens": 240,
        "reservation_id": "res-1",
    }
    assert result["debited_tokens"] == 240


@pytest.mark.asyncio
async def test_debit_omits_empty_reservation_id():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"debited_tokens": 0})

    await _client(handler).commerce_debit(
        "a-1",
        entitlement_id="",
        reserved_tokens=0,
        actual_tokens=0,
        reservation_id=None,
    )
    assert "reservation_id" not in seen["body"]
