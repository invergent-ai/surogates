"""The service account a case type's agent-step sessions run under."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from surogates.db.models import ServiceAccount
from surogates.tenant.auth.service_account import ServiceAccountStore

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_case_type_has_one_account_per_project(session_factory):
    org = await create_org(session_factory)
    other_org = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)

    ids = await asyncio.gather(
        *(store.case_type_account(org, "loan") for _ in range(5))
    )

    assert len(set(ids)) == 1
    account_id = ids[0]
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(
                    ServiceAccount.id,
                    ServiceAccount.kind,
                    ServiceAccount.name,
                    ServiceAccount.agent_id,
                ).where(ServiceAccount.org_id == org)
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        (account_id, "case_type", "case-type:loan", None)
    ]

    assert await store.case_type_account(org, "loan") == account_id
    other_type = await store.case_type_account(org, "claim")
    other_org_loan = await store.case_type_account(other_org, "loan")
    assert len({account_id, other_type, other_org_loan}) == 3
