"""Runs left running end, and old runs go with everything they made, on Postgres
with its foreign keys: a stand-in user and its sessions are referenced from
several tables, and only real constraints show the order is safe.

Set ``SUROGATE_TEST_PG_URL`` to an empty scratch database; the fixture drops
its ``public`` schema.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import (
    Base,
    ChannelIdentity,
    Event,
    FollowupEnrollmentRow,
    Mission,
    OutreachMessageRow,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ScheduledSession,
    Session,
    SessionLease,
    SimulationMedia,
    SimulationMessage,
    SimulationPerson,
    SimulationRun,
    Task,
    User,
)
from surogates.simulation import store
from tests.test_simulation_store import FakeVault, line, make_org, provision_into

URL = os.environ.get("SUROGATE_TEST_PG_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="set SUROGATE_TEST_PG_URL to a scratch Postgres database"),
]


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        await conn.exec_driver_sql("CREATE SCHEMA public")
        await conn.run_sync(Base.metadata.create_all)
        await apply_observability_ddl(conn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _provision(sf):
    """As the channel pipeline provisions: a user and its channel identity."""
    make_user = provision_into(sf)

    async def provision(session_factory, *, platform, platform_user_id, org_id, display_name=""):
        identity = await make_user(session_factory, platform=platform, platform_user_id=platform_user_id,
                                   org_id=org_id, display_name=display_name)
        async with sf() as db:
            db.add(ChannelIdentity(user_id=identity.user_id, platform=platform,
                                   platform_user_id=platform_user_id, org_id=org_id))
            await db.commit()
        return identity
    return provision


async def _run(sf, vault, org_id, *, created, status="ended"):
    run = await store.create_run(sf, vault, org_id=org_id, agent_id="a1", created_by="u1",
                                 routing_config={}, people=[line("Ana")], provision=_provision(sf))
    [person] = await store.people(sf, run.id)
    session_id = uuid.uuid4()
    async with sf() as db:
        db.add(Session(id=session_id, org_id=org_id, agent_id="a1", channel="whatsapp",
                       user_id=person.user_id, config={"simulation_run_id": str(run.id)}))
        await db.flush()
        # A sub-agent's session, its task, and a mission: none cascade.
        child_id = uuid.uuid4()
        db.add(Session(id=child_id, org_id=org_id, agent_id="a1", channel="whatsapp",
                       parent_id=session_id, user_id=person.user_id, config={}))
        await db.flush()
        task = Task(org_id=org_id, parent_session_id=session_id, current_session_id=child_id,
                    goal="g", status="done")
        db.add(task)
        db.add(Mission(org_id=org_id, session_id=session_id, user_id=person.user_id,
                       agent_id="a1", description="d", rubric="r"))
        db.add(ScheduledSession(org_id=org_id, agent_id="a1", user_id=person.user_id, name="n",
                                prompt="p", schedule={}, schedule_display="daily"))
        db.add(OutreachMessageRow(org_id=org_id, agent_id="a1", user_id=person.user_id,
                                  subject_user_id=person.user_id, kind="alert", platform="whatsapp", channel_identifier="x",
                                  text="t"))
        # A program step sent to them ahead of time: program rows have no foreign keys.
        occurrence = ProgramOccurrenceRow(program_id=uuid.uuid4(), org_id=org_id, agent_id="a1",
                                          scheduled_for=datetime.now(timezone.utc), skill_ref="s",
                                          status="simulated")
        db.add(occurrence)
        db.add(FollowupEnrollmentRow(org_id=org_id, agent_id="a1", user_id=person.user_id,
                                     protocol="post-op", enrolled_by="operator"))
        await db.flush()
        db.add(ProgramInvitationRow(occurrence_id=occurrence.id, program_id=occurrence.program_id,
                                    org_id=org_id, agent_id="a1", user_id=person.user_id,
                                    person_user_id=person.user_id, platform="whatsapp",
                                    platform_user_id=person.address, delivery_state="queued",
                                    response_state="not_started", escalation_state="none"))
        await db.flush()
        await db.execute(sa.update(Session).where(Session.id == child_id).values(task_id=task.id))
        db.add(Event(session_id=session_id, type="user.message", data={}))
        db.add(SessionLease(session_id=session_id, owner_id="w", lease_token=uuid.uuid4(),
                            expires_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.add(SimulationMedia(id=f"media{uuid.uuid4().hex[:8]}", run_id=run.id,
                               mime_type="image/png", data=b"png"))
        await db.execute(sa.update(SimulationRun).where(SimulationRun.id == run.id)
                         .values(created_at=created, status=status))
        await db.commit()
    await store.log_message(sf, run_id=run.id, person_id=person.id, direction="in", kind="text",
                            body={}, wamid=f"wamid.{run.id}")
    return run, person


async def _count(sf, model, *where) -> int:
    async with sf() as db:
        return await db.scalar(sa.select(sa.func.count()).select_from(model).where(*where))


async def test_sweep_ends_stale_runs_and_deletes_old_ones(pg):
    sf, vault = pg, FakeVault()
    now = datetime.now(timezone.utc)
    org_id = await make_org(sf)
    stale, _ = await _run(sf, vault, org_id, created=now - timedelta(hours=3), status="running")
    old, old_person = await _run(sf, vault, org_id, created=now - timedelta(days=91))
    recent, recent_person = await _run(sf, vault, org_id, created=now - timedelta(days=10))
    state = SimpleNamespace(session_factory=sf, vault=vault,
                            settings=SimpleNamespace(voice=SimpleNamespace(livekit_api_key="")))

    await store.sweep(state, now)

    assert (await store.get_run(sf, stale.id)).status == "ended"
    assert await store.get_run(sf, old.id) is None
    for model, column in ((SimulationPerson, SimulationPerson.run_id),
                          (SimulationMessage, SimulationMessage.run_id),
                          (SimulationMedia, SimulationMedia.run_id),
                          (User, User.simulation_run_id)):
        assert await _count(sf, model, column == old.id) == 0
    assert await _count(sf, Session, Session.user_id == old_person.user_id) == 0
    assert await _count(sf, Event, Event.user_id == old_person.user_id) == 0
    assert await _count(sf, ChannelIdentity, ChannelIdentity.user_id == old_person.user_id) == 0
    assert await _count(sf, Mission, Mission.user_id == old_person.user_id) == 0
    assert await _count(sf, ScheduledSession, ScheduledSession.user_id == old_person.user_id) == 0
    assert await _count(sf, OutreachMessageRow, OutreachMessageRow.user_id == old_person.user_id) == 0
    assert await _count(sf, FollowupEnrollmentRow, FollowupEnrollmentRow.user_id == old_person.user_id) == 0
    assert await _count(sf, ProgramInvitationRow, ProgramInvitationRow.user_id == old_person.user_id) == 0
    assert await _count(sf, ProgramOccurrenceRow) == 2  # the two runs still here keep theirs

    assert (await store.get_run(sf, recent.id)).status == "ended"
    assert await _count(sf, SimulationMessage, SimulationMessage.run_id == recent.id) == 1
    assert await _count(sf, Session, Session.user_id == recent_person.user_id) == 2
