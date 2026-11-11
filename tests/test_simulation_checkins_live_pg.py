"""Program check-ins reach a simulated person only when the builder asks for
the next one, from the run's own account, on Postgres: SQLite stores UUIDs
differently on ``users`` and the program tables, so the joins that tell a
simulated person from a real one only hold here.

Set ``SUROGATE_TEST_PG_URL`` to an empty scratch database; the fixture drops
its ``public`` schema.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import (
    Base,
    ChannelIdentity,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
    User,
)
from surogates.followup import store as followup_store
from surogates.programs.materialize import (
    identity_lookup_from,
    materialize_occurrence,
    send_queued_openers,
    wake_at_deadlines,
)
from surogates.simulation import store
from surogates.simulation.api import router
from tests.test_simulation_store import FakeVault, line, make_org
from tests.test_simulation_sweep_live_pg import _provision

URL = os.environ.get("SUROGATE_TEST_PG_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="set SUROGATE_TEST_PG_URL to a scratch Postgres database"),
]
TOKEN = "internal-token"
BUSINESS_NUMBER = "106540352242922"


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


def _config(**extra):
    return {
        "channel": "whatsapp", "channel_identifier": BUSINESS_NUMBER,
        "skill_ref": "post-op", "template_name": "daily", "template_language": "en_US",
        "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "times_local": ["09:00"], "timezone": "Europe/Bucharest",
        "audience": {"protocol": "post-op", "day_from": 0, "day_to": 30}, **extra,
    }


async def _world(sf, **config):
    """A running run with Ana, and a real person who is her contact, both
    enrolled in post-op, and one active Program for it."""
    org_id = await make_org(sf)
    run = await store.create_run(sf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1",
                                 routing_config={}, people=[line("Ana")], provision=_provision(sf))
    [ana] = await store.people(sf, run.id)
    real = User(org_id=org_id, email="real@x", display_name="Real", auth_provider="whatsapp",
                external_id="40700000001")
    async with sf() as db:
        db.add(real)
        await db.flush()
        db.add(ChannelIdentity(user_id=real.id, platform="whatsapp", platform_user_id="40700000001",
                               org_id=org_id,
                               platform_meta={"contact_permission": {"status": "granted"}}))
        db.add(ProgramScheduleRow(program_id=uuid.uuid4(), org_id=org_id, agent_id="a1",
                                  active=True, config=_config(**config)))
        await db.commit()
    for user_id in (ana.user_id, real.id):
        await followup_store.upsert_enrollment(
            sf, org_id=org_id, agent_id="a1", user_id=user_id, protocol="post-op",
            start_date=date.today(), phase=None, enrolled_by="operator",
        )
    await followup_store.link_contact(sf, org_id=org_id, agent_id="a1", person_user_id=ana.user_id,
                                      contact_user_id=real.id, relation="son", created_by="operator")
    return org_id, run, ana, real


async def _invitations(sf):
    async with sf() as db:
        return (await db.execute(sa.select(ProgramInvitationRow)
                                 .order_by(ProgramInvitationRow.created_at))).scalars().all()


def _client(sf):
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = sf
    app.state.settings = type("S", (), {"simulation": type("Sim", (), {"internal_token": TOKEN})})
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://simulation:8004",
                             headers={"Authorization": f"Bearer {TOKEN}"})


async def _next(sf, org_id, run, ana):
    async with _client(sf) as client:
        return await client.post(f"/internal/runs/{run.id}/people/{ana.id}/next-step",
                                 params={"org_id": str(org_id)})


async def test_the_scheduled_ticker_leaves_simulated_people_out(pg):
    org_id, run, ana, real = await _world(pg)
    async with pg() as db:
        schedule = (await db.execute(sa.select(ProgramScheduleRow))).scalar_one()
    slot = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0)

    await materialize_occurrence(schedule, session_factory=pg,
                                 identity_lookup=identity_lookup_from(pg), now=slot)

    assert [row.user_id for row in await _invitations(pg)] == [real.id]


async def test_next_step_writes_from_the_runs_own_account_to_her_alone(pg):
    org_id, run, ana, real = await _world(pg)

    r = await _next(pg, org_id, run, ana)

    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True, r.json()
    [row] = await _invitations(pg)
    assert (row.user_id, row.delivery_state) == (ana.user_id, "queued")
    assert (row.platform, row.channel_identifier) == (run.channel, run.phone_number_id)
    async with pg() as db:
        occurrence = (await db.execute(sa.select(ProgramOccurrenceRow))).scalar_one()
    assert occurrence.status == "simulated"
    # The real slot stays free for everyone else on the roster.
    assert occurrence.scheduled_for < datetime.fromisoformat(r.json()["due_at"])


async def test_pressing_again_reaches_the_following_step(pg):
    org_id, run, ana, _ = await _world(pg)

    first = (await _next(pg, org_id, run, ana)).json()
    async with pg() as db:
        await db.execute(sa.update(ProgramInvitationRow).values(response_state="completed"))
        await db.commit()
    second = (await _next(pg, org_id, run, ana)).json()

    assert second["sent"] is True, second
    assert datetime.fromisoformat(second["due_at"]) == (
        datetime.fromisoformat(first["due_at"]) + timedelta(days=1))


async def test_once_per_person_says_she_was_already_reached(pg):
    org_id, run, ana, _ = await _world(pg, once_per_person=True)

    assert (await _next(pg, org_id, run, ana)).json()["sent"] is True
    second = (await _next(pg, org_id, run, ana)).json()

    assert second == {**second, "sent": False, "reason": "Already reached in this program"}


async def test_quiet_hours_hold_real_people_but_not_simulated_ones(pg):
    now = datetime.now(timezone.utc)
    quiet = {"start": f"{(now - timedelta(hours=1)):%H:%M}", "end": f"{(now + timedelta(hours=1)):%H:%M}"}
    org_id, run, ana, real = await _world(pg, timezone="UTC",
                                          journey={"slug": "post-op", "quiet_hours": quiet})
    assert (await _next(pg, org_id, run, ana)).json()["sent"] is True
    [row] = await _invitations(pg)
    async with pg() as db:
        db.add(ProgramInvitationRow(
            occurrence_id=uuid.uuid4(), program_id=row.program_id, org_id=org_id, agent_id="a1",
            user_id=real.id, person_user_id=real.id, platform="whatsapp",
            platform_user_id="40700000001", channel_identifier=BUSINESS_NUMBER,
            delivery_state="queued", response_state="not_started", escalation_state="none",
        ))
        await db.commit()
    handed: list = []

    async def enqueue(invitation):
        handed.append(invitation.user_id)
        return 1

    await send_queued_openers(pg, identity_lookup=identity_lookup_from(pg), now=now, enqueue=enqueue)
    assert handed == [ana.user_id]

    async with pg() as db:
        await db.execute(sa.update(ProgramInvitationRow).values(
            response_state="no_reply_by_deadline", deadline_at=now - timedelta(minutes=1)))
        await db.commit()
    woken: list = []

    async def wake(invitation, *, event, config):
        woken.append(invitation.user_id)

    await wake_at_deadlines(pg, now=now, wake=wake)
    assert woken == [ana.user_id]


async def test_next_step_needs_an_enrollment(pg):
    org_id, run, ana, _ = await _world(pg)
    async with pg() as db:
        await db.execute(sa.update(followup_store.FollowupEnrollmentRow)
                         .where(followup_store.FollowupEnrollmentRow.user_id == ana.user_id)
                         .values(status="ended"))
        await db.commit()

    r = await _next(pg, org_id, run, ana)

    assert r.status_code == 409 and r.json()["detail"] == "Not enrolled in a program"
