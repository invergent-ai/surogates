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


# A public website's visitors arrive anonymously through the agent's one
# website channel. Before each got a ``visitor:`` partition their sessions
# had no user and no boundary, so the worker built the prompt from the
# agent's ``shared/`` memory and the memory tool was taken away.


def _website_session(config: dict, *, session_id: str = "s-1"):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=session_id, agent_id="support-bot", channel="website", config=config,
    )


def _visitor_key(session) -> str:
    """The key the worker loads the session's memory from, into its prompt too."""
    from surogates.orchestrator.worker import _build_r2_memory_keys

    return _build_r2_memory_keys(
        session=session, storage_key_prefix="agents/support-bot", user_id=None,
    )["memory"]


def test_two_visitors_never_share_memory_and_neither_reads_shared():
    from surogates.channels.memory_boundary import visitor_boundary

    ida = _website_session({"memory_boundary": visitor_boundary("support-bot", None, "cookie-ida")})
    other = _website_session({"memory_boundary": visitor_boundary("support-bot", None, "cookie-other")})

    keys = {_visitor_key(ida), _visitor_key(other)}

    assert len(keys) == 2
    assert all(key.startswith("agents/support-bot/boundaries/visitor:") for key in keys)
    assert "agents/support-bot/shared/memory.json" not in keys


def test_a_website_session_without_a_stamped_boundary_still_never_reads_shared():
    """A session bootstrapped before visitors had partitions gets one of its own."""
    first = _website_session({}, session_id="s-1")
    second = _website_session({}, session_id="s-2")

    assert session_memory_boundary(first).startswith("visitor:")
    assert _visitor_key(first) != _visitor_key(second)
    assert "/shared/" not in _visitor_key(first)


def test_a_visitor_boundary_is_memory_only_and_unforgeable_elsewhere():
    from types import SimpleNamespace

    boundary = "visitor:" + "a" * 32
    assert workspace_boundary(_website_session({"memory_boundary": boundary})) is None
    # Only a website session honours a visitor boundary: on any other
    # channel it names nothing, so it cannot open a visitor's memory.
    api = SimpleNamespace(id="s-3", agent_id="support-bot", channel="api", config={"memory_boundary": boundary})
    assert session_memory_boundary(api) is None


def test_a_visitor_keeps_the_memory_tool_but_never_skill_manage():
    from types import SimpleNamespace
    from uuid import uuid4

    from surogates.orchestrator.worker import _filter_effective_tools
    from surogates.tenant.context import TenantContext

    tenant = TenantContext(
        org_id=uuid4(), user_id=None, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/assets",
    )
    session = _website_session({"memory_boundary": "visitor:" + "a" * 32})
    session.task_id = None

    tools = _filter_effective_tools(
        tools={"memory", "skill_manage"}, tenant=tenant, session=session,
        use_api_for_harness_tools=True,
    )

    assert "memory" in tools
    assert "skill_manage" not in tools
