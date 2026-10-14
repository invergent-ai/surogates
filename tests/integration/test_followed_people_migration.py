"""Enrollments a relative made on their own account become the followed person's.

Run against Postgres: the migration is plain SQL in ``observability.sql`` and
runs on every ``surogates migrate``, so it must be idempotent on real data.
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

ORG = uuid.uuid4()


@pytest_asyncio.fixture(loop_scope="session")
async def engine(pg_url):
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    name = f"people_{uuid.uuid4().hex[:12]}"
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(pg_url.rsplit("/", 1)[0] + f"/{name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'o')"), {"id": ORG})
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _user(conn, name):
    user_id = uuid.uuid4()
    await conn.execute(text(
        "INSERT INTO users (id, org_id, email, display_name) VALUES (:id, :org, :e, :n)"
    ), {"id": user_id, "org": ORG, "e": f"{user_id}@x", "n": name})
    return user_id


async def _enrol(conn, user_id, protocol, patient_name, answered_by):
    enrollment_id = uuid.uuid4()
    await conn.execute(text(
        "INSERT INTO followup_enrollments (id, org_id, agent_id, user_id, protocol, enrolled_by,"
        " patient_name, answered_by) VALUES (:id, :org, 'a1', :u, :p, 'agent', :n, :a)"
    ), {"id": enrollment_id, "org": ORG, "u": user_id, "p": protocol, "n": patient_name, "a": answered_by})
    return enrollment_id


async def test_a_relatives_enrollment_moves_to_the_person_with_them_as_contact(engine):
    async with engine.begin() as conn:
        ana = await _user(conn, "Ana Pop")
        self_enrolled = await _user(conn, "Ion Ionescu")
        also_self = await _user(conn, "Maria Rus")
        moved = await _enrol(conn, ana, "post-tavi", "Ion Pop", "Ana, fiica")
        await _enrol(conn, self_enrolled, "post-tavi", "Ion Ionescu", None)
        # Their own number, a relative answering on it: the person themselves.
        await _enrol(conn, also_self, "post-tavi", "maria rus ", "Dan, fiul")
        program, other_program = uuid.uuid4(), uuid.uuid4()
        for pid, protocol in ((program, "post-tavi"), (other_program, "other")):
            await conn.execute(text(
                "INSERT INTO program_schedules (id, program_id, org_id, agent_id, active, config,"
                " next_run_at) VALUES (:id, :pid, :org, 'a1', true,"
                " CAST(:cfg AS jsonb), now())"
            ), {"id": uuid.uuid4(), "pid": pid, "org": ORG,
                "cfg": '{"audience": {"protocol": "%s"}}' % protocol})
        occ = uuid.uuid4()
        invitation = uuid.uuid4()
        await conn.execute(text(
            "INSERT INTO program_invitations (id, occurrence_id, program_id, org_id, agent_id,"
            " user_id, platform, platform_user_id) VALUES (:id, :occ, :pid, :org, 'a1', :u,"
            " 'whatsapp', '407')"
        ), {"id": invitation, "occ": occ, "pid": program, "org": ORG, "u": ana})
        escalation = uuid.uuid4()
        await conn.execute(text(
            "INSERT INTO escalations (id, org_id, agent_id, user_id, kind, reason)"
            " VALUES (:id, :org, 'a1', :u, 'alert', 'durere')"
        ), {"id": escalation, "org": ORG, "u": ana})

        await apply_observability_ddl(conn)
        await apply_observability_ddl(conn)

        person = (await conn.execute(text(
            "SELECT user_id FROM followup_enrollments WHERE id = :id"
        ), {"id": moved})).scalar_one()
        assert person != ana
        name, provider = (await conn.execute(text(
            "SELECT display_name, auth_provider FROM users WHERE id = :id"
        ), {"id": person})).one()
        assert (name, provider) == ("Ion Pop", "external")
        contacts = (await conn.execute(text(
            "SELECT person_user_id, contact_user_id, relation, status FROM followup_contacts"
        ))).all()
        assert [tuple(c) for c in contacts] == [(person, ana, "Ana, fiica", "confirmed")]
        bound = (await conn.execute(text(
            "SELECT count(*) FROM agent_users WHERE user_id = :u AND agent_id = 'a1'"
        ), {"u": person})).scalar_one()
        assert bound == 1
        assert (await conn.execute(text(
            "SELECT user_id, person_user_id FROM program_invitations WHERE id = :id"
        ), {"id": invitation})).one() == (ana, person)
        assert (await conn.execute(text(
            "SELECT user_id FROM escalations WHERE id = :id"
        ), {"id": escalation})).scalar_one() == person
        # The person themselves are never moved.
        owners = set((await conn.execute(text(
            "SELECT user_id FROM followup_enrollments WHERE id <> :id"
        ), {"id": moved})).scalars())
        assert owners == {self_enrolled, also_self}
        assert (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one() == 4


async def test_an_escalation_needs_no_person(engine):
    async with engine.begin() as conn:
        await apply_observability_ddl(conn)
        await conn.execute(text(
            "INSERT INTO escalations (id, org_id, agent_id, user_id, kind, reason)"
            " VALUES (:id, :org, 'a1', NULL, 'alert', 'x')"
        ), {"id": uuid.uuid4(), "org": ORG})


async def test_contacts_answers_and_routing_on_postgres(engine):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from surogates.db.models import ProgramInvitationRow, ProgramOccurrenceRow
    from surogates.followup import store
    from surogates.programs import funnel, inbound

    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await apply_observability_ddl(conn)
        person, ana = await _user(conn, "Ion"), await _user(conn, "Ana")
        await _enrol(conn, person, "p", "Ion", None)
    await store.link_contact(sf, org_id=ORG, agent_id="a1", person_user_id=person,
                             contact_user_id=ana, relation="fiica", created_by="t")
    enrollment = (await store.enrollments(sf, org_id=ORG, agent_id="a1", user_id=person))[0]
    assert [r.role for r in await store.recipients_for(sf, enrollment)] == ["person", "contact"]
    speaker = await store.speaker_for(sf, org_id=ORG, agent_id="a1", user_id=ana)
    assert speaker.person_user_id == person
    assert await store.shared_memory_boundary(
        sf, org_id=ORG, agent_id="a1", user_id=ana,
    ) == f"person:{person}"

    program = uuid.uuid4()
    async with sf() as db:
        occ = ProgramOccurrenceRow(program_id=program, org_id=ORG, agent_id="a1",
                                   scheduled_for=datetime.now(timezone.utc), skill_ref="s",
                                   status="fired")
        db.add(occ)
        await db.flush()
        rows = [
            ProgramInvitationRow(
                occurrence_id=occ.id, program_id=program, org_id=ORG, agent_id="a1",
                user_id=who, person_user_id=person, platform="whatsapp",
                platform_user_id=str(who)[:6], delivery_state="delivered",
                response_state="awaiting_reply", escalation_state="none", outbox_id=1,
            )
            for who in (person, ana)
        ]
        db.add_all(rows)
        await db.commit()
    await inbound.attach_reply(sf, rows[1], session_id=uuid.uuid4(), bundle_version=None)
    async with sf() as db:
        own = await db.get(ProgramInvitationRow, rows[0].id)
    assert own.response_state == "answered_by_other"
    f = (await funnel.step_funnel(sf, org_id=ORG, agent_id="a1", program_ids=[program],
                                  since=datetime.now(timezone.utc) - timedelta(days=1)))[program]
    assert (f["invited"], f["replied"]) == (1, 1)

    await store.update_settings(
        sf, org_id=ORG, agent_id="a1", alert_recipient_user_ids=[ana], senders={"slack": "T1"},
        team_permissions={str(ana): {"can": ["see_people", "message_person"]}},
        team_destinations=[{"platform": "slack", "channel_id": "C1", "receives": ["urgent"]}],
    )
    settings = await store.get_settings(sf, org_id=ORG, agent_id="a1")
    assert not store.can(settings, ana, "manage_team")
    queued, _ = await store.notify_team(sf, settings, org_id=ORG, agent_id="a1", kind="alert",
                                        text="x", created_by="t", topic="urgent")
    assert queued == 2
