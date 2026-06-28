"""Integration tests for the read-only agent-principal resolver."""
from __future__ import annotations

import uuid

import pytest

from surogates.runtime.agent_principal import (
    ServiceAccountPrincipal,
    make_cached_agent_principal_resolver,
)
from surogates.tenant.auth.service_account import ServiceAccountStore

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_resolves_provisioned_agent_principal(session_factory):
    org = await create_org(session_factory)
    agent_id = str(uuid.uuid4())
    issued = await ServiceAccountStore(session_factory).create(
        org_id=org, name=f"agent:{agent_id}", agent_id=agent_id,
    )
    resolve = make_cached_agent_principal_resolver(session_factory)

    p = await resolve(str(org), agent_id)
    assert isinstance(p, ServiceAccountPrincipal)
    assert p.id == issued.id and p.org_id == org and p.name == f"agent:{agent_id}"


async def test_unknown_agent_resolves_none(session_factory):
    org = await create_org(session_factory)
    resolve = make_cached_agent_principal_resolver(session_factory)
    assert await resolve(str(org), str(uuid.uuid4())) is None


async def test_invalidate_drops_cached_entry(session_factory):
    org = await create_org(session_factory)
    agent_id = str(uuid.uuid4())
    store = ServiceAccountStore(session_factory)
    issued = await store.create(org_id=org, name=f"agent:{agent_id}", agent_id=agent_id)
    resolve = make_cached_agent_principal_resolver(session_factory)

    assert await resolve(str(org), agent_id) is not None  # populates cache
    await store.revoke(service_account_id=issued.id, org_id=org)
    assert await resolve(str(org), agent_id) is not None, "stale until invalidated/TTL"

    resolve.cache.invalidate(f"{org}\x00{agent_id}")
    assert await resolve(str(org), agent_id) is None, "re-queries after invalidate"
