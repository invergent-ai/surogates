"""A followed person's step reaches them and every contact, on Postgres."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import Base, ProgramInvitationRow, User
from surogates.followup import store
from surogates.programs import funnel, inbound
from surogates.programs.materialize import materialize_occurrence

pytestmark = pytest.mark.asyncio(loop_scope="session")

ORG = uuid.uuid4()
AGENT = "a1"
SLOT = datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc)  # 08:00 in Bucharest


@pytest_asyncio.fixture(loop_scope="session")
async def sf(pg_url):
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    name = f"fanout_{uuid.uuid4().hex[:12]}"
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(pg_url.rsplit("/", 1)[0] + f"/{name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await apply_observability_ddl(conn)
        await conn.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'o')"), {"id": ORG})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


def _schedule():
    return SimpleNamespace(program_id=uuid.uuid4(), org_id=ORG, agent_id=AGENT, config={
        "channel": "whatsapp", "channel_identifier": "pn1",
        "template_name": "t", "template_language": "ro",
        "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "times_local": ["08:00"], "timezone": "Europe/Bucharest",
        "audience": {"protocol": "p", "day_to": 30},
        "journey": {"slug": "p", "senders": {"whatsapp": "pn1"}},
    })


async def _rows(sf, occurrence_id):
    async with sf() as db:
        return {r.user_id: r for r in (await db.execute(
            sa.select(ProgramInvitationRow).where(ProgramInvitationRow.occurrence_id == occurrence_id)
        )).scalars()}


async def test_a_step_reaches_the_person_and_each_contact_and_one_answer_closes_it(sf):
    person, ana, mihai = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        for uid, name in ((person, "Ion"), (ana, "Ana"), (mihai, "Mihai")):
            db.add(User(id=uid, org_id=ORG, email=f"{uid}@x", display_name=name))
        await db.commit()
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=person, protocol="p",
        start_date=date(2026, 10, 10), phase=None, enrolled_by="agent", patient_name="Ion",
    )
    for contact, relation in ((ana, "fiica"), (mihai, "fiul")):
        await store.link_contact(sf, org_id=ORG, agent_id=AGENT, person_user_id=person,
                                 contact_user_id=contact, relation=relation, created_by="t")
    phones = {person: "40700000001", ana: "40700000002", mihai: "40700000003"}

    async def lookup(org_id, platform, user_id):
        pid = phones.get(user_id) if platform == "whatsapp" else None
        return SimpleNamespace(
            platform_user_id=pid, platform_meta={"contact_permission": {"status": "granted"}},
        ) if pid else None

    schedule = _schedule()
    rows = await _rows(sf, await materialize_occurrence(
        schedule, session_factory=sf, identity_lookup=lookup, now=SLOT,
    ))
    assert set(rows) == {person, ana, mihai}
    assert {r.person_user_id for r in rows.values()} == {person}
    assert {r.delivery_state for r in rows.values()} == {"queued"}
    assert {r.platform_user_id for r in rows.values()} == set(phones.values())

    # The person's step is still open: the next slot asks none of them again.
    later = await _rows(sf, await materialize_occurrence(
        schedule, session_factory=sf, identity_lookup=lookup, now=SLOT + timedelta(days=1),
    ))
    assert {r.reason for r in later.values()} == {"An earlier step is still waiting for an answer"}

    async with sf() as db:
        await db.execute(sa.update(ProgramInvitationRow)
                         .where(ProgramInvitationRow.id.in_([r.id for r in rows.values()]))
                         .values(delivery_state="delivered", response_state="awaiting_reply"))
        await db.commit()
    await inbound.attach_reply(sf, rows[ana], session_id=uuid.uuid4(), bundle_version=None)
    after = await _rows(sf, rows[ana].occurrence_id)
    assert after[ana].response_state == "replied"
    assert {after[person].response_state, after[mihai].response_state} == {"answered_by_other"}
    assert after[person].reason == "Answered by Ana (fiica)"

    f = (await funnel.step_funnel(
        sf, org_id=ORG, agent_id=AGENT, program_ids=[schedule.program_id],
        since=SLOT - timedelta(days=1),
    ))[schedule.program_id]
    assert (f["invited"], f["replied"], f["skipped"]) == (2, 1, 1)


async def test_resolving_an_escalation_closes_its_inbox_item_on_postgres(sf):
    from surogates.db.models import Event, InboxItem, ServiceAccount, Session as SessionRow

    user_id, session_id, sa_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    escalation_id = await store.record_escalation(
        sf, org_id=ORG, agent_id=AGENT, user_id=None, kind="alert", reason="x",
    )
    async with sf() as db:
        db.add(User(id=user_id, org_id=ORG, email=f"{user_id}@x", display_name="P"))
        db.add(ServiceAccount(id=sa_id, org_id=ORG, name="inbox", token_hash="h", token_prefix="p"))
        db.add(SessionRow(id=session_id, org_id=ORG, agent_id=AGENT, channel="web", user_id=user_id))
        await db.flush()
        event = Event(session_id=session_id, org_id=ORG, type="followup.escalation", data={})
        db.add(event)
        await db.flush()
        for key in (f"escalation:-:{escalation_id.hex}", "escalation:-:other"):
            db.add(InboxItem(org_id=ORG, service_account_id=sa_id, session_id=session_id,
                             source_event_id=event.id if key.endswith(escalation_id.hex) else
                             (await _event(db, session_id)), kind="action_required",
                             title="t", payload={"escalation": key}))
        await db.commit()
    await store.resolve_escalation(sf, org_id=ORG, agent_id=AGENT, escalation_id=escalation_id)
    async with sf() as db:
        states = {i.payload["escalation"]: i.status for i in (await db.execute(sa.select(InboxItem))).scalars()}
    assert states == {f"escalation:-:{escalation_id.hex}": "acknowledged", "escalation:-:other": "pending"}


async def _event(db, session_id):
    from surogates.db.models import Event

    event = Event(session_id=session_id, org_id=ORG, type="followup.escalation", data={})
    db.add(event)
    await db.flush()
    return event.id
