"""Rehearsal tables round-trip on SQLite, and a user can belong to a run."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from surogates.db.models import (
    Base,
    Org,
    RehearsalMedia,
    RehearsalMessage,
    RehearsalPerson,
    RehearsalRun,
    User,
)
from tests.conftest import sqlite_tables

REHEARSAL_TABLES = (
    "orgs", "users", "channel_identities", "agent_users", "followup_enrollments",
    "rehearsal_runs", "rehearsal_people", "rehearsal_messages", "rehearsal_media",
)


@pytest_asyncio.fixture
async def rsf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=sqlite_tables(*REHEARSAL_TABLES))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_people_messages_media_round_trip(rsf):
    org_id = uuid.uuid4()
    async with rsf() as db:
        db.add(Org(id=org_id, name="Org"))
        await db.flush()
        run = RehearsalRun(
            org_id=org_id, agent_id="a1", created_by="u1", phone_number_id="9990000000000001",
            access_token="EAAx", app_secret="s", routing_config={"identity_policy": "shadow"},
        )
        db.add(run)
        await db.flush()
        person = RehearsalPerson(run_id=run.id, name="Ana", channel="whatsapp", address="999000000001")
        db.add(person)
        await db.flush()
        db.add(RehearsalMessage(run_id=run.id, person_id=person.id, direction="in", kind="text",
                                body={"text": {"body": "salut"}}, wamid="wamid.R1"))
        db.add(RehearsalMedia(id="media1", run_id=run.id, mime_type="image/png", data=b"png"))
        db.add(User(org_id=org_id, email="x@y", display_name="Ana", rehearsal_run_id=run.id))
        await db.commit()

    async with rsf() as db:
        stored = (await db.execute(select(RehearsalRun))).scalar_one()
        message = (await db.execute(select(RehearsalMessage))).scalar_one()
        user = (await db.execute(select(User))).scalar_one()
    assert stored.status == "running" and stored.ended_at is None
    assert message.id is not None and message.created_at.tzinfo is not None
    assert user.rehearsal_run_id == stored.id
