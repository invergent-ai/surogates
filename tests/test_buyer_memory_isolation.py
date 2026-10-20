"""Each buyer of a monetized agent gets its own memory partition.

The buy page forwards every buyer through the agent's one principal on the
``api`` channel, naming the buyer only in ``config.public_buyer_id``. With no
user and no boundary every buyer read and wrote ``shared/memory.json``, so one
buyer was told facts from another's chat.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from surogates.api.routes import sessions as sessions_route
from surogates.channels.memory_boundary import session_memory_boundary
from surogates.runtime.memory_protocol import memory_object_key
from surogates.storage.tenant import workspace_boundary
from surogates.tenant.auth.service_account import (
    KIND_AGENT_PRINCIPAL,
    KIND_API_KEY,
    ResolvedServiceAccount,
)
from tests.test_eval_session_seeding import _route_fixtures


def _caller(monkeypatch, kind: str) -> None:
    async def fake_get_by_id(self, service_account_id, org_id):
        return ResolvedServiceAccount(
            id=service_account_id, org_id=org_id, name="agent:support-bot",
            agent_id="support-bot", kind=kind,
        )

    monkeypatch.setattr(sessions_route.ServiceAccountStore, "get_by_id", fake_get_by_id)


async def _session(buyer_id: str):
    _, request, tenant, agent_runtime = _route_fixtures(
        "/v1/api/sessions", service_account=True,
    )
    return await sessions_route.create_api_session(
        sessions_route.CreateSessionRequest(config={"public_buyer_id": buyer_id}),
        request, tenant, agent_runtime,
    )


async def _memory_key(buyer_id: str) -> str:
    session = await _session(buyer_id)
    return memory_object_key(
        storage_key_prefix="agents/support-bot", user_id=None,
        boundary=session_memory_boundary(session), target="user",
    )


async def test_buyers_through_the_agent_principal_never_share_memory(monkeypatch):
    _caller(monkeypatch, KIND_AGENT_PRINCIPAL)

    ida, other = await _memory_key("buyer-ida"), await _memory_key("buyer-other")

    assert ida == "agents/support-bot/boundaries/buyer:buyer-ida/user.json"
    assert other == "agents/support-bot/boundaries/buyer:buyer-other/user.json"


async def test_a_buyer_chat_keeps_its_own_workspace(monkeypatch):
    _caller(monkeypatch, KIND_AGENT_PRINCIPAL)

    assert workspace_boundary(await _session("buyer-ida")) is None


async def test_an_api_key_naming_a_buyer_cannot_reach_its_memory(monkeypatch):
    _caller(monkeypatch, KIND_API_KEY)

    assert await _memory_key("buyer-ida") == "agents/support-bot/shared/user.json"


async def test_a_buyer_id_that_could_leave_the_partition_is_refused(monkeypatch):
    _caller(monkeypatch, KIND_AGENT_PRINCIPAL)

    with pytest.raises(HTTPException) as exc:
        await _memory_key("../users/x")
    assert exc.value.status_code == 422
