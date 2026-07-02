"""CredentialVault round-trips a service-account-scoped secret, isolated from
the same name at user and org scope."""

from __future__ import annotations

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from surogates.tenant.credentials import CredentialVault
from tests.integration.conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _mk_sa(session_factory, org_id):
    from sqlalchemy import text
    sid = uuid4()
    async with session_factory() as db:
        await db.execute(
            text("INSERT INTO service_accounts (id, org_id, name, token_hash, "
                 "token_prefix) VALUES (:id, :o, :n, :h, :p)"),
            {
                "id": sid,
                "o": org_id,
                "n": f"agent:{sid}",
                "h": f"h:{sid}",
                "p": str(sid)[:8],
            },
        )
        await db.commit()
    return sid


async def test_service_account_scope_roundtrip_and_isolation(session_factory):
    vault = CredentialVault(session_factory, Fernet.generate_key())
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    sa_id = await _mk_sa(session_factory, org_id)

    await vault.store(org_id, "byo_key", "user-secret", user_id=user_id)
    await vault.store(org_id, "byo_key", "agent-secret", service_account_id=sa_id)
    await vault.store(org_id, "byo_key", "org-secret")

    assert await vault.retrieve(org_id, "byo_key", service_account_id=sa_id) == "agent-secret"
    assert await vault.resolve_ref("vault://byo_key", org_id=org_id, service_account_id=sa_id) == "agent-secret"
    assert await vault.retrieve(org_id, "byo_key", user_id=user_id) == "user-secret"
    assert await vault.retrieve(org_id, "byo_key") == "org-secret"


async def test_service_account_list_delete_and_list_all(session_factory):
    vault = CredentialVault(session_factory, Fernet.generate_key())
    org_id = await create_org(session_factory)
    sa_id = await _mk_sa(session_factory, org_id)

    await vault.store(org_id, "agent_key", "agent-secret", service_account_id=sa_id)
    await vault.store(org_id, "org_key", "org-secret")

    assert await vault.list_names(org_id, service_account_id=sa_id) == ["agent_key"]
    assert await vault.delete(org_id, "agent_key", service_account_id=sa_id) is True
    assert await vault.retrieve(org_id, "agent_key", service_account_id=sa_id) is None

    await vault.store(org_id, "agent_key", "agent-secret", service_account_id=sa_id)
    rows, total = await vault.list_all(service_account_id=sa_id)
    assert total == 1
    assert rows == [(org_id, None, sa_id, "agent_key")]


async def test_rejects_both_principals(session_factory):
    vault = CredentialVault(session_factory, Fernet.generate_key())
    org_id = await create_org(session_factory)
    with pytest.raises(ValueError):
        await vault.store(org_id, "x", "v", user_id=uuid4(), service_account_id=uuid4())
