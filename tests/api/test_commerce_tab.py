"""Plan & tokens tab routes: the harness fronts ops for the signed-in
web user, translating identities and failures."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from surogates.api.routes import commerce
from surogates.runtime import AgentRuntimeContext


def _ctx() -> AgentRuntimeContext:
    return AgentRuntimeContext(
        agent_id="a-1",
        org_id="o-1",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
    )


class _FakePlatform:
    def __init__(self, *, summary=None, checkout=None, error=None):
        self._summary = summary
        self._checkout = checkout
        self._error = error
        self.calls: list[tuple] = []

    async def commerce_summary(self, agent_id, **kw):
        self.calls.append(("summary", agent_id, kw))
        if self._error:
            raise self._error
        return dict(self._summary)

    async def commerce_checkout(self, agent_id, **kw):
        self.calls.append(("checkout", agent_id, kw))
        if self._error:
            raise self._error
        return dict(self._checkout)


def _request(platform):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(platform_client=platform)),
    )


_TENANT = SimpleNamespace(user_id="u-1", org_id="o-1")
_SUMMARY = {
    "mode": "subscription_required",
    "buy_url": "https://buy",
    "offers": [{"id": "off-1"}],
    "entitlement": {"topup_token_remaining": 5},
}


@pytest.mark.asyncio
async def test_overview_marks_purchasable_for_firebase_user(monkeypatch):
    async def ident(request, tenant):
        return {"firebase_uid": "uid-1", "email": "u@example.com", "name": "U"}

    monkeypatch.setattr(commerce, "firebase_buyer_identity", ident)
    platform = _FakePlatform(summary=_SUMMARY)
    out = await commerce.commerce_overview(
        _request(platform), tenant=_TENANT, agent_runtime=_ctx(),
    )
    assert out["purchasable"] is True
    assert out["entitlement"]["topup_token_remaining"] == 5
    assert platform.calls[0][2]["firebase_uid"] == "uid-1"


@pytest.mark.asyncio
async def test_overview_disables_buying_without_firebase_identity(
    monkeypatch,
):
    async def ident(request, tenant):
        return None

    monkeypatch.setattr(commerce, "firebase_buyer_identity", ident)
    platform = _FakePlatform(summary=_SUMMARY)
    out = await commerce.commerce_overview(
        _request(platform), tenant=_TENANT, agent_runtime=_ctx(),
    )
    assert out["purchasable"] is False
    assert out["entitlement"] is None


@pytest.mark.asyncio
async def test_checkout_translates_ops_refusals(monkeypatch):
    async def ident(request, tenant):
        return {"firebase_uid": "uid-1", "email": None, "name": None}

    monkeypatch.setattr(commerce, "firebase_buyer_identity", ident)
    refusal = httpx.HTTPStatusError(
        "400",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(400, json={"detail": "Offer is not active"}),
    )
    platform = _FakePlatform(error=refusal)
    with pytest.raises(HTTPException) as err:
        await commerce.commerce_checkout(
            commerce.CommerceCheckoutRequest(offer_id="off-1"),
            _request(platform),
            tenant=_TENANT,
            agent_runtime=_ctx(),
        )
    assert err.value.status_code == 400
    assert err.value.detail == "Offer is not active"


@pytest.mark.asyncio
async def test_checkout_refuses_operator_accounts(monkeypatch):
    async def ident(request, tenant):
        return None

    monkeypatch.setattr(commerce, "firebase_buyer_identity", ident)
    platform = _FakePlatform(checkout={"url": "x"})
    with pytest.raises(HTTPException) as err:
        await commerce.commerce_checkout(
            commerce.CommerceCheckoutRequest(offer_id="off-1"),
            _request(platform),
            tenant=_TENANT,
            agent_runtime=_ctx(),
        )
    assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_checkout_returns_url(monkeypatch):
    async def ident(request, tenant):
        return {"firebase_uid": "uid-1", "email": "u@example.com", "name": "U"}

    monkeypatch.setattr(commerce, "firebase_buyer_identity", ident)
    platform = _FakePlatform(checkout={"url": "https://stripe/xyz"})
    out = await commerce.commerce_checkout(
        commerce.CommerceCheckoutRequest(offer_id="off-1"),
        _request(platform),
        tenant=_TENANT,
        agent_runtime=_ctx(),
    )
    assert out.url == "https://stripe/xyz"
