"""A simulation run's stand-in users reach neither the Users view nor a Program's audience."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from surogates.db.agent_users import visible_users_filter
from surogates.db.models import Org, User
from surogates.followup import store as followup_store
from tests.test_simulation_models import rsf  # noqa: F401


@pytest.mark.asyncio
async def test_simulation_users_are_hidden(rsf):  # noqa: F811
    org_id = uuid.uuid4()
    real, staged = uuid.uuid4(), uuid.uuid4()
    async with rsf() as db:
        db.add(Org(id=org_id, name="Org"))
        await db.flush()
        db.add(User(id=real, org_id=org_id, email="real@x", display_name="Real"))
        db.add(User(id=staged, org_id=org_id, email="staged@x", display_name="Staged",
                    simulation_run_id=uuid.uuid4()))
        await db.commit()

    async with rsf() as db:
        listed = (await db.execute(
            select(User.id).where(User.org_id == org_id, visible_users_filter(org_id, "a1"))
        )).scalars().all()
    assert listed == [real]



@pytest.mark.asyncio
async def test_enrollments_skip_simulation_users():
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
    assert "users.simulation_run_id IS NOT NULL" in seen[0]
    await followup_store.enrollments(
        lambda: Capture(), org_id=uuid.uuid4(), agent_id="a1", simulation_run_id=uuid.uuid4(),
    )
    assert "users.simulation_run_id IS NOT NULL" not in seen[1]
    assert "users.simulation_run_id = " in seen[1]
    await followup_store.enrollments(
        lambda: Capture(), org_id=uuid.uuid4(), agent_id="a1", user_id=uuid.uuid4(),
    )
    assert "simulation_run_id" not in seen[2]


@pytest.mark.asyncio
async def test_team_messages_about_a_simulated_person_stay_in_the_run(rsf):  # noqa: F811
    from surogates.simulation import store as simulation_store

    org_id = uuid.uuid4()
    doctor, patient, staged = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with rsf() as db:
        db.add(Org(id=org_id, name="Org"))
        await db.flush()
        db.add(User(id=doctor, org_id=org_id, email="d@x", display_name="Dr Ana"))
        db.add(User(id=patient, org_id=org_id, email="p@x", display_name="Real patient"))
        db.add(User(id=staged, org_id=org_id, email="s@x", display_name="Staged",
                    simulation_run_id=uuid.uuid4()))
        await db.commit()

    def alert(subject):
        return followup_store.queue_outreach(
            rsf, org_id=org_id, agent_id="a1", user_id=doctor, platform="whatsapp",
            channel_identifier="100", kind="alert", text="Bruise at the puncture site",
            template_name=None, template_language=None, subject_user_id=subject,
        )

    kept, sent = await alert(staged), await alert(patient)
    assert (kept.state, kept.reason) == ("simulated", followup_store.SIMULATED)
    assert sent.state == "queued"
    team = await simulation_store.team_messages(rsf, [staged])
    assert [(m["to"], m["text"], m["platform"]) for m in team] == [
        ("Dr Ana", "Bruise at the puncture site", "whatsapp")]
