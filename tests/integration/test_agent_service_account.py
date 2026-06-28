"""Schema-level tests for the per-agent service-account column."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from surogates.db.models import ServiceAccount
from surogates.tenant.auth.service_account import (
    ServiceAccountStore,
    _reset_caches,
    hash_token,
)

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _sa(org_id, *, agent_id=None, suffix=""):
    return ServiceAccount(
        org_id=org_id,
        name=f"agent:{agent_id or 'none'}{suffix}",
        token_hash=uuid.uuid4().hex,
        token_prefix="surg_sk_aaaaaaaa",
        agent_id=agent_id,
    )


async def test_agent_id_partial_unique_rejects_duplicate(session_factory):
    org = await create_org(session_factory)
    agent_id = str(uuid.uuid4())
    async with session_factory() as db:
        db.add(_sa(org, agent_id=agent_id))
        await db.commit()
    with pytest.raises(IntegrityError):
        async with session_factory() as db:
            db.add(_sa(org, agent_id=agent_id, suffix="-dup"))
            await db.commit()


async def test_null_agent_id_allows_many(session_factory):
    org = await create_org(session_factory)
    async with session_factory() as db:
        db.add(_sa(org, suffix="-1"))
        db.add(_sa(org, suffix="-2"))
        await db.commit()  # partial index → no conflict on NULL agent_id


async def test_create_with_agent_id_and_lookup(session_factory):
    _reset_caches()
    org = await create_org(session_factory)
    agent_id = str(uuid.uuid4())
    store = ServiceAccountStore(session_factory)

    issued = await store.create(org_id=org, name=f"agent:{agent_id}", agent_id=agent_id)
    assert issued.agent_id == agent_id

    found = await store.get_by_agent_id(org, agent_id)
    assert found is not None and found.id == issued.id
    assert await store.get_by_agent_id(org, str(uuid.uuid4())) is None


async def test_rotate_token_for_agent_id(session_factory):
    _reset_caches()
    org = await create_org(session_factory)
    agent_id = str(uuid.uuid4())
    store = ServiceAccountStore(session_factory)
    issued = await store.create(org_id=org, name=f"agent:{agent_id}", agent_id=agent_id)

    # The old token resolves before rotation.
    assert await store.get_by_token(issued.token) is not None

    rotated = await store.rotate_token_for_agent_id(org_id=org, agent_id=agent_id)
    assert rotated is not None and rotated.id == issued.id
    assert rotated.token != issued.token

    _reset_caches()  # drop the 60s TTL cache so the DB state is read fresh
    assert await store.get_by_token(issued.token) is None, "old token no longer resolves"
    assert await store.get_by_token(rotated.token) is not None, "new token resolves"


async def test_rotate_unknown_agent_returns_none(session_factory):
    org = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    assert await store.rotate_token_for_agent_id(org_id=org, agent_id=str(uuid.uuid4())) is None


async def test_rotate_clear_revoked_reactivates(session_factory):
    _reset_caches()
    org = await create_org(session_factory)
    agent_id = str(uuid.uuid4())
    store = ServiceAccountStore(session_factory)
    issued = await store.create(org_id=org, name=f"agent:{agent_id}", agent_id=agent_id)
    await store.revoke(service_account_id=issued.id, org_id=org)
    assert await store.get_by_agent_id(org, agent_id) is None, "revoked SA does not resolve"

    rotated = await store.rotate_token_for_agent_id(
        org_id=org, agent_id=agent_id, clear_revoked=True,
    )
    assert rotated is not None
    assert await store.get_by_agent_id(org, agent_id) is not None, "re-activated"
