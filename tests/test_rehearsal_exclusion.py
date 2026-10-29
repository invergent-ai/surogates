"""A rehearsal run's stand-in users reach neither the Users view nor a Program's audience."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from surogates.db.agent_users import visible_users_filter
from surogates.db.models import Org, User
from surogates.followup import store as followup_store
from tests.test_rehearsal_models import rsf  # noqa: F401


@pytest.mark.asyncio
async def test_rehearsal_users_are_hidden(rsf):  # noqa: F811
    org_id = uuid.uuid4()
    real, staged = uuid.uuid4(), uuid.uuid4()
    async with rsf() as db:
        db.add(Org(id=org_id, name="Org"))
        await db.flush()
        db.add(User(id=real, org_id=org_id, email="real@x", display_name="Real"))
        db.add(User(id=staged, org_id=org_id, email="staged@x", display_name="Staged",
                    rehearsal_run_id=uuid.uuid4()))
        await db.commit()

    async with rsf() as db:
        listed = (await db.execute(
            select(User.id).where(User.org_id == org_id, visible_users_filter(org_id, "a1"))
        )).scalars().all()
    assert listed == [real]



@pytest.mark.asyncio
async def test_enrollments_skip_rehearsal_users():
    # GUID and UUID columns store differently on SQLite, so the correlated
    # check is asserted on the Postgres statement instead of run.
    seen = []

    class Capture:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement):
            seen.append(str(statement.compile(dialect=postgresql.dialect())))

            class Rows:
                def scalars(self):
                    return self

                def all(self):
                    return []
            return Rows()

    await followup_store.enrollments(lambda: Capture(), org_id=uuid.uuid4(), agent_id="a1")
    assert "users.rehearsal_run_id IS NOT NULL" in seen[0]
