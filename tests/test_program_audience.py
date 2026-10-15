"""Programs that speak to a protocol's enrolled people instead of a roster."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from surogates.db.models import ProgramInvitationRow
from surogates.followup import store
from surogates.programs.materialize import materialize_occurrence

ORG = uuid.uuid4()
AGENT = "a1"
# 23:30 UTC is already the next day in Bucharest (UTC+3 in October).
NOW = datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc)
LOCAL_TODAY = date(2026, 10, 12)


def _schedule(audience, *, tz="Europe/Bucharest"):
    return SimpleNamespace(
        program_id=uuid.uuid4(),
        org_id=ORG,
        agent_id=AGENT,
        config={
            "channel": "whatsapp",
            "channel_identifier": "pn1",
            "skill_ref": "check-in",
            "template_name": "morning",
            "template_language": "ro",
            "response_deadline_hours": 10,
            "timezone": tz,
            "audience": audience,
            "users": [],
        },
    )


def _lookup(phones):
    async def lookup(org_id, platform, user_id):
        phone = phones.get(user_id)
        if phone is None:
            return None
        return SimpleNamespace(
            platform_user_id=phone,
            platform_meta={"contact_permission": {"status": "granted"}},
        )
    return lookup


async def _rows(sf, occ):
    async with sf() as db:
        return (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.occurrence_id == occ)
            )
        ).scalars().all()


async def _enrol(sf, *, start=None, phase=None, protocol="post-op"):
    user = uuid.uuid4()
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol=protocol,
        start_date=start, phase=phase, enrolled_by="agent",
    )
    return user


@pytest.mark.asyncio
async def test_only_people_inside_the_day_window_are_invited(sf):
    on_day_0 = await _enrol(sf, start=LOCAL_TODAY)
    on_day_30 = await _enrol(sf, start=LOCAL_TODAY - timedelta(days=30))
    on_day_31 = await _enrol(sf, start=LOCAL_TODAY - timedelta(days=31))
    no_date = await _enrol(sf)
    other = await _enrol(sf, start=LOCAL_TODAY, protocol="other")
    phones = {u: f"4070000000{i}" for i, u in enumerate(
        [on_day_0, on_day_30, on_day_31, no_date, other])}

    occ = await materialize_occurrence(
        _schedule({"protocol": "post-op", "day_from": 0, "day_to": 30}),
        session_factory=sf, identity_lookup=_lookup(phones), now=NOW,
    )
    rows = await _rows(sf, occ)
    assert {r.user_id for r in rows} == {on_day_0, on_day_30}
    assert all(r.delivery_state == "queued" for r in rows)


@pytest.mark.asyncio
async def test_the_day_is_counted_in_the_programs_timezone(sf):
    # Started "yesterday" in UTC terms, but it is already the next local day:
    # day 1 in Bucharest, day 0 in UTC.
    user = await _enrol(sf, start=LOCAL_TODAY - timedelta(days=1))
    lookup = _lookup({user: "40700000001"})
    day_zero_only = {"protocol": "post-op", "day_from": 0, "day_to": 0}

    local = await materialize_occurrence(
        _schedule(day_zero_only), session_factory=sf,
        identity_lookup=lookup, now=NOW,
    )
    utc = await materialize_occurrence(
        _schedule(day_zero_only, tz="UTC"), session_factory=sf,
        identity_lookup=lookup, now=NOW,
    )
    assert await _rows(sf, local) == []
    assert [r.user_id for r in await _rows(sf, utc)] == [user]


@pytest.mark.asyncio
async def test_paused_and_ended_enrollments_are_not_invited(sf):
    active = await _enrol(sf)
    paused = await _enrol(sf)
    await store.update_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=paused, protocol="post-op",
        status="paused", status_reason="in hospital",
    )
    occ = await materialize_occurrence(
        _schedule({"protocol": "post-op"}), session_factory=sf,
        identity_lookup=_lookup({active: "1", paused: "2"}), now=NOW,
    )
    assert [r.user_id for r in await _rows(sf, occ)] == [active]


@pytest.mark.asyncio
async def test_phase_filter(sf):
    titrating = await _enrol(sf, phase="titration")
    maintaining = await _enrol(sf, phase="maintenance")
    occ = await materialize_occurrence(
        _schedule({"protocol": "post-op", "phases": ["titration"]}),
        session_factory=sf,
        identity_lookup=_lookup({titrating: "1", maintaining: "2"}), now=NOW,
    )
    assert [r.user_id for r in await _rows(sf, occ)] == [titrating]


@pytest.mark.asyncio
async def test_silence_filter_reads_the_last_message_to_this_sender(sf):
    quiet = await _enrol(sf)
    chatty = await _enrol(sf)
    # Enrolled long ago, so "never wrote" alone would make both silent.
    from surogates.db.models import FollowupEnrollmentRow
    async with sf() as db:
        await db.execute(
            sa.update(FollowupEnrollmentRow).values(
                created_at=NOW - timedelta(days=20),
            )
        )
        await db.commit()
    await store.record_inbound(
        sf, org_id=ORG, platform="whatsapp", platform_user_id="1",
        channel_identifier="pn1", at=NOW - timedelta(days=5),
    )
    await store.record_inbound(
        sf, org_id=ORG, platform="whatsapp", platform_user_id="2",
        channel_identifier="pn1", at=NOW - timedelta(hours=2),
    )
    # A recent message to a different sender does not count.
    await store.record_inbound(
        sf, org_id=ORG, platform="whatsapp", platform_user_id="1",
        channel_identifier="other-sender", at=NOW - timedelta(hours=1),
    )
    occ = await materialize_occurrence(
        _schedule({"protocol": "post-op", "silent_days": 3}),
        session_factory=sf,
        identity_lookup=_lookup({quiet: "1", chatty: "2"}), now=NOW,
    )
    assert [r.user_id for r in await _rows(sf, occ)] == [quiet]


@pytest.mark.asyncio
async def test_due_but_unreachable_is_recorded_not_dropped(sf):
    user = await _enrol(sf)
    occ = await materialize_occurrence(
        _schedule({"protocol": "post-op"}), session_factory=sf,
        identity_lookup=_lookup({}), now=NOW,
    )
    [row] = await _rows(sf, occ)
    assert row.user_id == user
    assert row.delivery_state == "skipped"


@pytest.mark.asyncio
async def test_an_unreadable_audience_messages_nobody(sf):
    await _enrol(sf)
    occ = await materialize_occurrence(
        _schedule({"protocol": "post-op", "day_from": 9, "day_to": 1}),
        session_factory=sf, identity_lookup=_lookup({}), now=NOW,
    )
    assert await _rows(sf, occ) == []


@pytest.mark.asyncio
async def test_a_fixed_roster_is_unchanged(sf):
    user = uuid.uuid4()
    schedule = _schedule(None)
    schedule.config["users"] = [str(user)]
    occ = await materialize_occurrence(
        schedule, session_factory=sf,
        identity_lookup=_lookup({user: "1"}), now=NOW,
    )
    [row] = await _rows(sf, occ)
    assert row.user_id == user
    assert row.delivery_state == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("once", [True, False])
async def test_a_once_per_person_step_reaches_each_person_once_inside_its_window(sf, once):
    reached = await _enrol(sf, start=LOCAL_TODAY - timedelta(days=21))
    skipped = await _enrol(sf, start=LOCAL_TODAY - timedelta(days=21))
    schedule = _schedule({"protocol": "post-op", "day_from": 21, "day_to": 35})
    if once:
        schedule.config["once_per_person"] = True
    phones = {reached: "40700000001", skipped: "40700000002"}

    first = await materialize_occurrence(
        schedule, session_factory=sf, identity_lookup=_lookup(phones), now=NOW,
    )
    async with sf() as db:
        await db.execute(
            sa.update(ProgramInvitationRow)
            .where(ProgramInvitationRow.user_id == skipped)
            .values(delivery_state="skipped")
        )
        await db.execute(
            sa.update(ProgramInvitationRow)
            .where(ProgramInvitationRow.user_id == reached)
            .values(delivery_state="delivered", response_state="completed")
        )
        await db.commit()
    assert {r.user_id for r in await _rows(sf, first)} == {reached, skipped}

    week_later = await materialize_occurrence(
        schedule, session_factory=sf, identity_lookup=_lookup(phones),
        now=NOW + timedelta(days=7),
    )
    again = {r.user_id for r in await _rows(sf, week_later)}
    assert again == ({skipped} if once else {reached, skipped})


@pytest.mark.asyncio
async def test_a_person_reached_only_through_a_contact_has_no_failed_row_of_their_own(sf):
    person = await _enrol(sf, start=LOCAL_TODAY)
    alone = await _enrol(sf, start=LOCAL_TODAY)
    caregiver = uuid.uuid4()
    await store.link_contact(sf, org_id=ORG, agent_id=AGENT, person_user_id=person,
                             contact_user_id=caregiver, relation="daughter", created_by="t")
    occ = await materialize_occurrence(
        _schedule({"protocol": "post-op", "day_from": 0, "day_to": 30}),
        session_factory=sf, identity_lookup=_lookup({caregiver: "40700000009"}), now=NOW,
    )
    rows = {(r.user_id, r.person_user_id): (r.delivery_state, r.reason) for r in await _rows(sf, occ)}
    assert rows == {
        (caregiver, person): ("queued", None),
        # Nobody reaches this one: the reason says why nothing went out.
        (alone, alone): ("skipped", "No whatsapp identity"),
    }


@pytest.mark.asyncio
async def test_a_person_keeps_their_skipped_row_when_no_contact_copy_reaches_anyone(sf):
    # Their contact is unreachable; the other's contact is followed too and
    # gets one row only, their own.
    unreached = await _enrol(sf, start=LOCAL_TODAY)
    shared = await _enrol(sf, start=LOCAL_TODAY)
    caregiver_without_phone = uuid.uuid4()
    followed_caregiver = await _enrol(sf, start=LOCAL_TODAY)
    await store.link_contact(sf, org_id=ORG, agent_id=AGENT, person_user_id=unreached,
                             contact_user_id=caregiver_without_phone, relation="son", created_by="t")
    await store.link_contact(sf, org_id=ORG, agent_id=AGENT, person_user_id=shared,
                             contact_user_id=followed_caregiver, relation="wife", created_by="t")
    occ = await materialize_occurrence(
        _schedule({"protocol": "post-op", "day_from": 0, "day_to": 30}),
        session_factory=sf, identity_lookup=_lookup({followed_caregiver: "40700000008"}), now=NOW,
    )
    rows = {(r.user_id, r.person_user_id): r.delivery_state for r in await _rows(sf, occ)}
    assert rows[(unreached, unreached)] == "skipped"
    assert rows[(shared, shared)] == "skipped"
    assert rows[(followed_caregiver, followed_caregiver)] == "queued"


@pytest.mark.asyncio
async def test_a_copy_got_as_someone_elses_contact_is_not_ones_own_step(sf):
    person = await _enrol(sf, start=LOCAL_TODAY - timedelta(days=21))
    caregiver = await _enrol(sf, start=LOCAL_TODAY - timedelta(days=21))
    schedule = _schedule({"protocol": "post-op", "day_from": 21, "day_to": 35})
    schedule.config["once_per_person"] = True
    async with sf() as db:
        db.add(ProgramInvitationRow(
            occurrence_id=uuid.uuid4(), program_id=schedule.program_id, org_id=ORG, agent_id=AGENT,
            user_id=caregiver, person_user_id=person, platform="whatsapp",
            platform_user_id="40700000002", delivery_state="delivered",
            response_state="completed", escalation_state="none",
        ))
        await db.commit()
    occ = await materialize_occurrence(
        schedule, session_factory=sf,
        identity_lookup=_lookup({person: "40700000001", caregiver: "40700000002"}), now=NOW,
    )
    assert {r.user_id for r in await _rows(sf, occ)} == {caregiver}
