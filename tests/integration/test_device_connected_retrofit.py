"""The ``devices.connected_at`` retrofit, against a database from before the column.

A device that connected before the column existed must count as connected: one
the desktop then takes back would otherwise be deleted with its history, and
its sign-in unbound instead of ended.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import Base

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

SEEN = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture(loop_scope="session")
async def engine(pg_url):
    """A database of this module's own: dropping a column must not reach the suite's."""
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    name = f"connected_{uuid.uuid4().hex[:12]}"
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


async def device(conn, org_id, user_id, last_seen_at) -> uuid.UUID:
    device_id = uuid.uuid4()
    await conn.execute(text(
        "INSERT INTO devices (id, org_id, agent_id, user_id, name, token_hash, token_prefix, last_seen_at)"
        " VALUES (:id, :org, 'agent', :user, 'Laptop', :hash, 'surg_dev_', :seen)"
    ), {"id": device_id, "org": org_id, "user": user_id, "hash": uuid.uuid4().hex, "seen": last_seen_at})
    return device_id


async def connected(conn, device_id) -> datetime | None:
    return await conn.scalar(text("SELECT connected_at FROM devices WHERE id = :id"), {"id": device_id})


async def test_a_device_seen_before_the_column_counts_as_connected_and_a_replay_changes_nothing(engine):
    sessions = async_sessionmaker(engine)
    org_id = await create_org(sessions)
    user_id = await create_user(sessions, org_id)
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE devices DROP COLUMN connected_at"))
        seen = await device(conn, org_id, user_id, SEEN)
        unseen = await device(conn, org_id, user_id, None)
    async with engine.begin() as conn:
        await apply_observability_ddl(conn)
    async with engine.begin() as conn:
        assert await connected(conn, seen) == SEEN
        assert await connected(conn, unseen) is None
        # Seen from now on, as the link's token check does, but never welcomed.
        await conn.execute(text("UPDATE devices SET last_seen_at = now() WHERE id = :id"), {"id": unseen})
    async with engine.begin() as conn:
        await apply_observability_ddl(conn)
    async with engine.begin() as conn:
        assert await connected(conn, unseen) is None
