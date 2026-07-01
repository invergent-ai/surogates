"""A credential can be scoped to a service account, distinct from a user."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from surogates.db.models import Credential
from tests.integration.conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _mk_sa(session_factory, org_id):
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


async def test_service_account_scoped_credential_persists(session_factory):
    org_id = await create_org(session_factory)
    sa_id = await _mk_sa(session_factory, org_id)
    async with session_factory() as db:
        db.add(Credential(org_id=org_id, service_account_id=sa_id,
                          name="byo_key", value_enc=b"x"))
        await db.commit()
    async with session_factory() as db:
        row = (await db.execute(
            text("SELECT service_account_id, user_id FROM credentials "
                 "WHERE org_id=:o AND name='byo_key'"), {"o": org_id}
        )).one()
    assert str(row[0]) == str(sa_id)
    assert row[1] is None


async def test_cannot_set_both_user_and_service_account(session_factory):
    org_id = await create_org(session_factory)
    sa_id = await _mk_sa(session_factory, org_id)
    from tests.integration.conftest import create_user
    uid = await create_user(session_factory, org_id)
    with pytest.raises(IntegrityError):
        async with session_factory() as db:
            db.add(Credential(org_id=org_id, user_id=uid,
                              service_account_id=sa_id, name="bad", value_enc=b"x"))
            await db.commit()
