"""Program check-ins reach the people of a running simulation, on Postgres:
SQLite stores UUIDs differently on ``users`` and ``followup_enrollments``, so
the join that tells a simulated person from a real one only holds here.

Set ``SUROGATE_TEST_PG_URL`` to an empty scratch database; the fixture drops
its ``public`` schema.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime

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
    SimulationRun,
    User,
)
from surogates.followup import store as followup_store
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


async def _world(sf):
    """A running run with Ana, and a real person, both enrolled in post-op."""
    org_id = await make_org(sf)
    run = await store.create_run(sf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1",
                                 routing_config={}, people=[line("Ana")], provision=_provision(sf))
    [ana] = await store.people(sf, run.id)
    real = User(org_id=org_id, email="real@x", display_name="Real", auth_provider="whatsapp",
                external_id="40700000001")
    async with sf() as db:
        db.add(real)
        await db.execute(sa.update(ChannelIdentity).where(ChannelIdentity.user_id == ana.user_id)
                         .values(platform_meta={"contact_permission": {"status": "granted"}}))
        await db.commit()
    for user_id in (ana.user_id, real.id):
        await followup_store.upsert_enrollment(
            sf, org_id=org_id, agent_id="a1", user_id=user_id, protocol="post-op",
            start_date=date.today(), phase=None, enrolled_by="operator",
        )
    return org_id, run, ana, real


async def _roster(sf, org_id, **kw):
    return {e.user_id for e in await followup_store.enrollments(
        sf, org_id=org_id, agent_id="a1", **kw)}


async def test_running_simulations_join_the_roster_only_when_asked(pg):
    org_id, run, ana, real = await _world(pg)

    assert await _roster(pg, org_id) == {real.id}
    assert await _roster(pg, org_id, include_running_simulations=True) == {real.id, ana.user_id}

    async with pg() as db:
        await db.execute(sa.update(SimulationRun).where(SimulationRun.id == run.id)
                         .values(status="ended"))
        await db.commit()
    assert await _roster(pg, org_id, include_running_simulations=True) == {real.id}


def _client(sf):
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = sf
    app.state.settings = type("S", (), {"simulation": type("Sim", (), {"internal_token": TOKEN})})
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://simulation:8004",
                             headers={"Authorization": f"Bearer {TOKEN}"})


async def test_next_step_sends_the_persons_next_check_in_now(pg):
    org_id, run, ana, real = await _world(pg)
    program_id = uuid.uuid4()
    async with pg() as db:
        db.add(ProgramScheduleRow(program_id=program_id, org_id=org_id, agent_id="a1", active=True, config={
            "channel": "whatsapp", "channel_identifier": run.phone_number_id,
            "skill_ref": "post-op", "template_name": "daily", "template_language": "en_US",
            "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "times_local": ["09:00"], "timezone": "Europe/Bucharest",
            "audience": {"protocol": "post-op", "day_from": 0, "day_to": 30},
        }))
        await db.commit()

    async with _client(pg) as client:
        r = await client.post(f"/internal/runs/{run.id}/people/{ana.id}/next-step",
                              params={"org_id": str(org_id)})
    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True

    async with pg() as db:
        rows = (await db.execute(sa.select(ProgramInvitationRow))).scalars().all()
        [stamped] = (await db.execute(sa.select(ProgramOccurrenceRow.scheduled_for))).scalars().all()
    assert [(row.user_id, row.delivery_state) for row in rows] == [(ana.user_id, "queued")]
    # The real slot stays free for everyone else on the roster.
    assert stamped < datetime.fromisoformat(r.json()["due_at"])


async def test_next_step_needs_an_enrollment(pg):
    org_id, run, ana, _ = await _world(pg)
    async with pg() as db:
        await db.execute(sa.update(followup_store.FollowupEnrollmentRow)
                         .where(followup_store.FollowupEnrollmentRow.user_id == ana.user_id)
                         .values(status="ended"))
        await db.commit()

    async with _client(pg) as client:
        r = await client.post(f"/internal/runs/{run.id}/people/{ana.id}/next-step",
                              params={"org_id": str(org_id)})
    assert r.status_code == 409 and r.json()["detail"] == "Not enrolled in a program"
