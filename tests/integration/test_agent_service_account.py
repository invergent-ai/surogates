"""Schema-level tests for the per-agent service-account column."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from surogates.db.models import ServiceAccount

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
