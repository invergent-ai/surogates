"""The Plan & Usage tab starts the agent's Free plan through ops."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from surogates.api.routes import commerce
from surogates.runtime import AgentRuntimeContext

_CTX = AgentRuntimeContext(
    agent_id="a-1",
    org_id="o-1",
    project_id="p-1",
    enabled=True,
    config_version=1,
    storage_key_prefix="p-1/a-1",
)
_TENANT = SimpleNamespace(user_id="u-1", org_id="o-1")
_BODY = commerce.CommerceCheckoutRequest(offer_id="off-free")


class _Platform:
    def __init__(self, error=None):
        self.error = error
        self.calls: list = []

    async def commerce_free_plan(self, agent_id, **kw):
        self.calls.append((agent_id, kw))
        if self.error:
            raise self.error
        return {"subscription_status": "free", "period_token_remaining": 3000}


def _request(platform):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(platform_client=platform)),
    )


async def _firebase(request, tenant):
    return {"firebase_uid": "uid-1", "email": "u@example.com", "name": "U"}


@pytest.mark.asyncio
async def test_starts_the_free_plan_for_the_signed_in_user(monkeypatch):
    monkeypatch.setattr(commerce, "firebase_buyer_identity", _firebase)
    platform = _Platform()
    out = await commerce.commerce_free_plan(
        _BODY, _request(platform), tenant=_TENANT, agent_runtime=_CTX,
    )
    assert out["subscription_status"] == "free"
    assert platform.calls == [
        ("a-1", {"offer_id": "off-free", "firebase_uid": "uid-1",
                 "email": "u@example.com", "name": "U"}),
    ]


@pytest.mark.asyncio
async def test_operator_accounts_cannot_claim(monkeypatch):
    async def none(request, tenant):
        return None

    monkeypatch.setattr(commerce, "firebase_buyer_identity", none)
    with pytest.raises(HTTPException) as err:
        await commerce.commerce_free_plan(
            _BODY, _request(_Platform()), tenant=_TENANT, agent_runtime=_CTX,
        )
    assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_ops_refusal_keeps_its_reason(monkeypatch):
    monkeypatch.setattr(commerce, "firebase_buyer_identity", _firebase)
    refusal = httpx.HTTPStatusError(
        "409",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(409, json={"detail": "already_subscribed"}),
    )
    with pytest.raises(HTTPException) as err:
        await commerce.commerce_free_plan(
            _BODY, _request(_Platform(refusal)), tenant=_TENANT, agent_runtime=_CTX,
        )
    assert err.value.detail == "already_subscribed"
