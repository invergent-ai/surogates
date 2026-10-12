"""The ``workstreams`` retrofit, against a database from before a project could work on a computer.

A project made before the columns existed was made in the cloud: it gains
them empty, and stays there.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import Base

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def engine(pg_url):
    """A database of this module's own: dropping a column must not reach the suite's."""
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    name = f"workstream_folder_{uuid.uuid4().hex[:12]}"
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


async def test_a_project_from_before_projects_had_folders_stays_in_the_cloud(engine):
    sessions = async_sessionmaker(engine)
    org_id = await create_org(sessions)
    user_id = await create_user(sessions, org_id)
    project_id, master_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE workstreams DROP COLUMN device_id, DROP COLUMN folder"))
        await conn.execute(text(
            "INSERT INTO sessions (id, org_id, user_id, agent_id, channel, status, config)"
            " VALUES (:id, :org, :user, 'agent', 'web', 'active', '{}')"
        ), {"id": master_id, "org": org_id, "user": user_id})
        await conn.execute(text(
            "INSERT INTO workstreams (id, org_id, agent_id, user_id, name, master_session_id)"
            " VALUES (:id, :org, 'agent', :user, 'Q3', :master)"
        ), {"id": project_id, "org": org_id, "user": user_id, "master": master_id})
    for _ in range(2):
        async with engine.begin() as conn:
            await apply_observability_ddl(conn)
    async with engine.begin() as conn:
        row = (await conn.execute(
            text("SELECT device_id, folder FROM workstreams WHERE id = :id"), {"id": project_id},
        )).one()
    assert tuple(row) == (None, None)
