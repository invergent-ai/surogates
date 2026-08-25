"""Agent-bound API keys for the OpenAI-compatible channel.

Two invariants this file exists to hold:

1. An agent may carry MANY API keys but exactly ONE principal.  Before
   ``kind`` existed the partial unique index capped an agent at one service
   account of any sort, which made a second API key impossible.
2. A key minted for one agent cannot drive another.  The agent is resolved
   from the ``Host`` header, independently of authentication, so the binding
   has to be carried on the principal and checked at the route.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from surogates.api.routes._shared import require_token_binds_agent
from surogates.tenant.auth.service_account import (
    KIND_AGENT_PRINCIPAL,
    KIND_API_KEY,
    KIND_SERVICE,
    ServiceAccountStore,
)
from surogates.tenant.context import TenantContext

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _ctx(*, agent_id: str | None, sa_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(
        org_id=uuid.uuid4(),
        user_id=None,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/assets",
        service_account_id=sa_id or uuid.uuid4(),
        service_account_agent_id=agent_id,
    )


# ---------------------------------------------------------------------------
# cardinality
# ---------------------------------------------------------------------------

async def test_an_agent_can_carry_many_api_keys(session_factory):
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    agent_id = f"agent-{uuid.uuid4()}"

    keys = [
        await store.create(
            org_id=org_id, name=f"key-{i}", agent_id=agent_id, kind=KIND_API_KEY,
        )
        for i in range(3)
    ]

    assert len({k.id for k in keys}) == 3
    assert all(k.agent_id == agent_id for k in keys)
    assert all(k.kind == KIND_API_KEY for k in keys)
    assert len({k.token for k in keys}) == 3


async def test_the_principal_is_still_capped_at_one_per_agent(session_factory):
    """The relaxed index must not relax the invariant it was protecting."""
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    agent_id = f"agent-{uuid.uuid4()}"

    await store.create(org_id=org_id, name="principal", agent_id=agent_id)

    with pytest.raises(Exception):
        await store.create(org_id=org_id, name="principal-2", agent_id=agent_id)

    # ...and API keys alongside it are still fine.
    key = await store.create(
        org_id=org_id, name="k", agent_id=agent_id, kind=KIND_API_KEY,
    )
    assert key.kind == KIND_API_KEY


async def test_create_infers_the_historical_kind_from_its_arguments(
    session_factory,
):
    """Existing callers pass no ``kind`` and must keep their old meaning."""
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)

    org_scoped = await store.create(org_id=org_id, name="pipeline")
    assert org_scoped.kind == KIND_SERVICE
    assert org_scoped.agent_id is None

    principal = await store.create(
        org_id=org_id, name="p", agent_id=f"agent-{uuid.uuid4()}",
    )
    assert principal.kind == KIND_AGENT_PRINCIPAL


# ---------------------------------------------------------------------------
# the principal lookups must not see API keys
# ---------------------------------------------------------------------------

async def test_principal_lookup_ignores_api_keys(session_factory):
    """``get_by_agent_id`` answers 'the agent's machine identity'.

    An API key shares ``(org_id, agent_id)`` with the principal, so without a
    kind filter this returns an arbitrary row — and the control plane would
    hand an operator's customer-facing key to the runtime as the agent's own
    identity.
    """
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    agent_id = f"agent-{uuid.uuid4()}"

    api_key = await store.create(
        org_id=org_id, name="customer-key", agent_id=agent_id, kind=KIND_API_KEY,
    )
    principal = await store.create(
        org_id=org_id, name="principal", agent_id=agent_id,
    )

    found = await store.get_by_agent_id(org_id, agent_id)
    assert found is not None
    assert found.id == principal.id
    assert found.id != api_key.id
    assert found.kind == KIND_AGENT_PRINCIPAL


async def test_rotation_never_rotates_an_api_key(session_factory):
    """Rotating the principal must not silently break a live integration."""
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    agent_id = f"agent-{uuid.uuid4()}"

    api_key = await store.create(
        org_id=org_id, name="customer-key", agent_id=agent_id, kind=KIND_API_KEY,
    )
    await store.create(org_id=org_id, name="principal", agent_id=agent_id)

    rotated = await store.rotate_token_for_agent_id(
        org_id=org_id, agent_id=agent_id,
    )
    assert rotated is not None
    assert rotated.id != api_key.id

    # The customer's key still authenticates.
    assert await store.get_by_token(api_key.token) is not None


# ---------------------------------------------------------------------------
# resolution carries the binding
# ---------------------------------------------------------------------------

async def test_resolution_carries_the_agent_binding_on_every_path(
    session_factory,
):
    """by-token and by-id must agree, or the guard silently opens up."""
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    agent_id = f"agent-{uuid.uuid4()}"

    issued = await store.create(
        org_id=org_id, name="k", agent_id=agent_id, kind=KIND_API_KEY,
    )

    by_token = await store.get_by_token(issued.token)
    assert by_token is not None
    assert by_token.agent_id == agent_id
    assert by_token.kind == KIND_API_KEY

    by_id = await store.get_by_id(issued.id, org_id)
    assert by_id is not None
    assert by_id.agent_id == agent_id

    org_scoped = await store.create(org_id=org_id, name="ops-chat")
    resolved = await store.get_by_token(org_scoped.token)
    assert resolved is not None
    assert resolved.agent_id is None


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------

def test_a_bound_key_is_refused_against_another_agent():
    with pytest.raises(HTTPException) as exc:
        require_token_binds_agent(_ctx(agent_id="agent-a"), "agent-b")
    assert exc.value.status_code == 403
    assert "bound to a different agent" in exc.value.detail


def test_a_bound_key_passes_against_its_own_agent():
    require_token_binds_agent(_ctx(agent_id="agent-a"), "agent-a")


def test_an_org_scoped_token_is_unchanged():
    """The control plane's own machine identities must keep org-wide reach."""
    require_token_binds_agent(_ctx(agent_id=None), "any-agent-at-all")


# ---------------------------------------------------------------------------
# listing + revocation, the operator-facing surface
# ---------------------------------------------------------------------------

async def test_listing_shows_only_this_agents_live_api_keys(session_factory):
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    agent_a = f"agent-{uuid.uuid4()}"
    agent_b = f"agent-{uuid.uuid4()}"

    live = await store.create(
        org_id=org_id, name="live", agent_id=agent_a, kind=KIND_API_KEY,
    )
    dead = await store.create(
        org_id=org_id, name="dead", agent_id=agent_a, kind=KIND_API_KEY,
    )
    await store.create(org_id=org_id, name="principal", agent_id=agent_a)
    await store.create(
        org_id=org_id, name="other", agent_id=agent_b, kind=KIND_API_KEY,
    )
    await store.revoke_api_key_for_agent(
        service_account_id=dead.id, org_id=org_id, agent_id=agent_a,
    )

    listed = await store.list_api_keys_for_agent(org_id, agent_a)
    ids = {r.id for r in listed}
    assert ids == {live.id}, "principal, revoked, and other agents' keys must not appear"

    with_revoked = await store.list_api_keys_for_agent(
        org_id, agent_a, include_revoked=True,
    )
    assert {r.id for r in with_revoked} == {live.id, dead.id}


async def test_revoking_is_scoped_to_the_agent_and_spares_the_principal(
    session_factory,
):
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    agent_a = f"agent-{uuid.uuid4()}"
    agent_b = f"agent-{uuid.uuid4()}"

    principal = await store.create(
        org_id=org_id, name="principal", agent_id=agent_a,
    )
    key_a = await store.create(
        org_id=org_id, name="a", agent_id=agent_a, kind=KIND_API_KEY,
    )
    key_b = await store.create(
        org_id=org_id, name="b", agent_id=agent_b, kind=KIND_API_KEY,
    )

    # An operator on agent_a cannot revoke agent_b's key by guessing its id.
    assert await store.revoke_api_key_for_agent(
        service_account_id=key_b.id, org_id=org_id, agent_id=agent_a,
    ) is False
    assert await store.get_by_token(key_b.token) is not None

    # ...nor take the agent off the air by revoking its principal.
    assert await store.revoke_api_key_for_agent(
        service_account_id=principal.id, org_id=org_id, agent_id=agent_a,
    ) is False
    assert await store.get_by_agent_id(org_id, agent_a) is not None

    # Its own key revokes, immediately, and only once.
    assert await store.revoke_api_key_for_agent(
        service_account_id=key_a.id, org_id=org_id, agent_id=agent_a,
    ) is True
    assert await store.get_by_token(key_a.token) is None
    assert await store.revoke_api_key_for_agent(
        service_account_id=key_a.id, org_id=org_id, agent_id=agent_a,
    ) is False
