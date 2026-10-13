"""The ``followup_enrollments`` patient retrofit, against a table from before it.

Existing enrollments come back nameless, answered by the patient, with an
exact start date: nothing about them was known to be otherwise.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import Base

pytestmark = pytest.mark.asyncio(loop_scope="session")

COLUMNS = ("patient_name", "answered_by", "start_date_approximate")


@pytest_asyncio.fixture(loop_scope="session")
async def engine(pg_url):
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    name = f"followup_{uuid.uuid4().hex[:12]}"
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(pg_url.rsplit("/", 1)[0] + f"/{name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_existing_enrollments_gain_the_columns(engine):
    async with engine.begin() as conn:
        for column in COLUMNS:
            await conn.execute(text(f"ALTER TABLE followup_enrollments DROP COLUMN {column}"))
        await conn.execute(text(
            "INSERT INTO followup_enrollments (id, org_id, agent_id, user_id, protocol, enrolled_by)"
            " VALUES (:id, :org, 'a1', :user, 'tavi', 'agent')"
        ), {"id": uuid.uuid4(), "org": uuid.uuid4(), "user": uuid.uuid4()})
        await apply_observability_ddl(conn)
        await apply_observability_ddl(conn)
        row = (await conn.execute(text(
            "SELECT patient_name, answered_by, start_date_approximate FROM followup_enrollments"
        ))).one()
    assert tuple(row) == (None, None, False)
