"""Each end user an API key serves gets their own memory partition.

A service that holds an agent's API key, such as Surogate Portal, serves many
people through it. With no user and no boundary every one of them read and
wrote the agent's ``shared/`` memory, so one person was told what another had
said. A session that names its ``end_user`` gets ``enduser:<key>:<digest>``,
derived by the server from the key's own service account, so no other key can
name its way into those people's memory.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.api.routes import sessions as sessions_route
from surogates.channels.memory_boundary import session_memory_boundary
from surogates.runtime.memory_protocol import memory_object_key
from surogates.storage.tenant import workspace_boundary
from surogates.tenant.auth.service_account import KIND_API_KEY, ResolvedServiceAccount
from tests.test_eval_session_seeding import _route_fixtures


def _caller(monkeypatch) -> None:
    async def fake_get_by_id(self, service_account_id, org_id):
        return ResolvedServiceAccount(
            id=service_account_id, org_id=org_id, name="key:portal",
            agent_id="support-bot", kind=KIND_API_KEY,
        )

    monkeypatch.setattr(sessions_route.ServiceAccountStore, "get_by_id", fake_get_by_id)


async def _session(config: dict, *, fixtures=None):
    _, request, tenant, agent_runtime = fixtures or _route_fixtures(
        "/v1/api/sessions", service_account=True,
    )
    return await sessions_route.create_api_session(
        sessions_route.CreateSessionRequest(config=config), request, tenant, agent_runtime,
    )


def _memory_key(session) -> str:
    return memory_object_key(
        storage_key_prefix="agents/support-bot", user_id=None,
        boundary=session_memory_boundary(session), target="user",
    )


async def test_two_end_users_of_one_key_never_share_memory(monkeypatch):
    _caller(monkeypatch)
    fixtures = _route_fixtures("/v1/api/sessions", service_account=True)

    ana = _memory_key(await _session({"end_user": "tenant-1:ana"}, fixtures=fixtures))
    bo = _memory_key(await _session({"end_user": "tenant-1:bo"}, fixtures=fixtures))
    again = _memory_key(await _session({"end_user": "tenant-1:ana"}, fixtures=fixtures))

    assert ana != bo
    assert ana == again
    assert ana.startswith("agents/support-bot/boundaries/enduser:")
    assert "/shared/" not in ana and "/shared/" not in bo


async def test_the_same_end_user_under_another_key_is_someone_else(monkeypatch):
    _caller(monkeypatch)

    first = _memory_key(await _session({"end_user": "ana"}))
    second = _memory_key(await _session({"end_user": "ana"}))

    assert first != second


async def test_a_key_that_names_no_end_user_keeps_the_agents_shared_memory(monkeypatch):
    _caller(monkeypatch)

    assert _memory_key(await _session({})) == "agents/support-bot/shared/user.json"


@pytest.mark.parametrize("end_user", ["", "   ", "x" * 201, "ana\nbo", 7])
async def test_an_end_user_that_is_not_a_plain_name_is_refused(monkeypatch, end_user):
    _caller(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await _session({"end_user": end_user})
    assert exc.value.status_code == 422


async def test_an_end_users_boundary_is_memory_only_and_cannot_be_named(monkeypatch):
    _caller(monkeypatch)

    session = await _session({"end_user": "ana"})
    assert workspace_boundary(session) is None
    # A client naming the boundary itself gets the agent's shared memory, never someone's.
    forged = await _session({"memory_boundary": session_memory_boundary(session)})
    assert _memory_key(forged) == "agents/support-bot/shared/user.json"


def test_a_sub_agent_of_an_end_users_session_keeps_their_memory():
    """A child inherits the boundary on its own channel: it never falls back to the shared memory."""
    boundary = f"enduser:{uuid4()}:" + "a" * 32
    for channel in ("delegation", "worker"):
        child = SimpleNamespace(
            id="s-2", agent_id="support-bot", channel=channel, config={"memory_boundary": boundary},
        )
        assert session_memory_boundary(child) == boundary
