"""A step inside a journey: its channel per person, fallback, quiet hours,
each person's zone and the journey's end."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from surogates.db.models import (
    ChannelIdentity,
    FollowupEnrollmentRow,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
)
from surogates.followup import store as followup_store
from surogates.programs import journey
from surogates.programs.delivery import apply_status_callback, record_outbox_result
from surogates.programs.materialize import materialize_occurrence, send_queued_openers

ORG = uuid.uuid4()
AGENT = "a1"
# Monday 2026-10-12 05:00 UTC is 08:00 in Bucharest.
SLOT = datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc)
SENDERS = {"whatsapp": "pn1", "telegram": "bot1", "slack": "T1"}


def _granted(pid):
    return SimpleNamespace(
        platform_user_id=pid, platform_meta={"contact_permission": {"status": "granted"}},
    )


def _lookup(reachable: dict):
    """``{(platform, user_id): platform_user_id}`` the person can be reached on."""
    async def lookup(org_id, platform, user_id):
        pid = reachable.get((platform, user_id))
        return _granted(pid) if pid else None
    return lookup


def _schedule(users=(), *, audience=None, **journey_settings):
    config = {
        "channel": "whatsapp", "channel_identifier": "pn1",
        "template_name": "t", "template_language": "ro",
        "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "times_local": ["08:00"], "timezone": "Europe/Bucharest",
        "users": [str(u) for u in users], "audience": audience,
        "journey": {"slug": "onboarding", "senders": SENDERS, **journey_settings},
    }
    return SimpleNamespace(program_id=uuid.uuid4(), org_id=ORG, agent_id=AGENT, config=config)


async def _rows(sf, occurrence_id):
    async with sf() as db:
        return {
            r.user_id: r for r in (await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.occurrence_id == occurrence_id)
            )).scalars()
        }


async def _fire(sf, schedule, lookup, now=SLOT):
    return await _rows(sf, await materialize_occurrence(
        schedule, session_factory=sf, identity_lookup=lookup, now=now,
    ))


# ---------------------------------------------------------------------------
# Channel per person
# ---------------------------------------------------------------------------


async def _enrol(sf, user, **extra):
    async with sf() as db:
        db.add(FollowupEnrollmentRow(
            org_id=ORG, agent_id=AGENT, user_id=user, protocol="onboarding",
            status="active", enrolled_by="agent", **extra,
        ))
        await db.commit()


AUDIENCE = {"protocol": "onboarding"}


@pytest.mark.asyncio
async def test_preferred_then_last_written_then_the_steps_own_channel(sf):
    pref, wrote, plain = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    everywhere = {}
    for user, n in ((pref, 1), (wrote, 2), (plain, 3)):
        everywhere.update({
            ("whatsapp", user): f"407{n}", ("telegram", user): f"55{n}", ("slack", user): f"U{n}",
        })
    await _enrol(sf, pref, preferred_channel="slack")
    await _enrol(sf, wrote)
    await _enrol(sf, plain)
    await followup_store.record_inbound(
        sf, org_id=ORG, platform="telegram", platform_user_id="552",
        channel_identifier="bot1", at=SLOT - timedelta(hours=1),
    )
    await followup_store.record_inbound(
        sf, org_id=ORG, platform="whatsapp", platform_user_id="4072",
        channel_identifier="pn1", at=SLOT - timedelta(days=2),
    )
    rows = await _fire(sf, _schedule(audience=AUDIENCE), _lookup(everywhere))

    assert (rows[pref].platform, rows[pref].platform_user_id, rows[pref].channel_identifier) == (
        "slack", "U1", "T1",
    )
    assert (rows[wrote].platform, rows[wrote].channel_identifier) == ("telegram", "bot1")
    assert rows[plain].platform == "whatsapp"
    # The rest are tried once each if the send fails.
    assert sorted(rows[plain].fallback_platforms) == ["slack", "telegram"]


@pytest.mark.asyncio
async def test_a_preferred_channel_nobody_can_send_on_is_passed_over(sf):
    a = uuid.uuid4()
    await _enrol(sf, a, preferred_channel="slack")
    rows = await _fire(sf, _schedule(audience=AUDIENCE), _lookup({("telegram", a): "555"}))
    assert (rows[a].platform, rows[a].fallback_platforms) == ("telegram", None)


@pytest.mark.asyncio
async def test_nobody_reachable_is_skipped_visibly(sf):
    a = uuid.uuid4()
    rows = await _fire(sf, _schedule([a]), _lookup({}))
    assert rows[a].delivery_state == "skipped"
    assert rows[a].reason == "No reachable channel"


@pytest.mark.asyncio
async def test_without_a_journey_the_step_keeps_its_single_channel(sf):
    a = uuid.uuid4()
    schedule = _schedule([a])
    del schedule.config["journey"]
    rows = await _fire(sf, schedule, _lookup({("telegram", a): "555"}))
    assert rows[a].platform == "whatsapp"
    assert rows[a].reason == "No whatsapp identity"


# ---------------------------------------------------------------------------
# Fallback and delivery
# ---------------------------------------------------------------------------


async def _fallback_invitation(sf, *, state, **extra):
    user = uuid.uuid4()
    schedule = _schedule()
    async with sf() as db:
        db.add(ProgramScheduleRow(
            program_id=schedule.program_id, org_id=ORG, agent_id=AGENT,
            active=True, config=schedule.config,
        ))
        db.add(ChannelIdentity(
            org_id=ORG, user_id=user, platform="telegram", platform_user_id="555",
            platform_meta={"contact_permission": {"status": "granted"}},
        ))
        occ = ProgramOccurrenceRow(
            program_id=schedule.program_id, org_id=ORG, agent_id=AGENT,
            scheduled_for=SLOT, skill_ref="s", status="fired",
        )
        db.add(occ)
        await db.flush()
        row = ProgramInvitationRow(
            occurrence_id=occ.id, program_id=schedule.program_id, org_id=ORG,
            agent_id=AGENT, user_id=user, platform="whatsapp", platform_user_id="4070",
            channel_identifier="pn1", delivery_state=state, response_state="awaiting_reply",
            escalation_state="none", outbox_id=7, fallback_platforms=["slack", "telegram"],
            **extra,
        )
        db.add(row)
        await db.commit()
        return row


async def _reload(sf, row_id):
    async with sf() as db:
        return await db.get(ProgramInvitationRow, row_id)


@pytest.mark.asyncio
async def test_a_failed_send_moves_to_the_next_reachable_channel(sf):
    row = await _fallback_invitation(sf, state="queued")
    assert await record_outbox_result(sf, 7, provider_message_id=None, error="131026 undeliverable")
    moved = await _reload(sf, row.id)
    assert (moved.platform, moved.platform_user_id, moved.channel_identifier) == ("telegram", "555", "bot1")
    assert (moved.delivery_state, moved.response_state, moved.outbox_id) == ("queued", "not_started", None)
    assert moved.fallback_platforms is None
    # Slack had no identity for them: tried and passed over, on the record.
    assert [(a["platform"], a["state"]) for a in moved.attempts] == [("whatsapp", "failed"), ("slack", "skipped")]


@pytest.mark.asyncio
async def test_an_opener_the_send_pass_could_not_hand_over_moves_on_next_tick(sf):
    row = await _fallback_invitation(sf, state="failed")
    assert await journey.advance_fallbacks(sf, now=SLOT) == 1
    assert (await _reload(sf, row.id)).platform == "telegram"
    # Once each: nothing is left to try.
    assert await journey.advance_fallbacks(sf, now=SLOT) == 0


@pytest.mark.asyncio
async def test_a_read_receipt_records_read(sf):
    row = await _fallback_invitation(sf, state="accepted", provider_message_id="wamid.1")
    assert await apply_status_callback(sf, provider_message_id="wamid.1", status="read", reason=None)
    seen = await _reload(sf, row.id)
    assert seen.read_at is not None and seen.delivery_state == "delivered"
    assert seen.platform == "whatsapp"


@pytest.mark.asyncio
async def test_a_successful_post_on_a_channel_without_receipts_is_delivered(sf, make_invitation):
    inv = await make_invitation(
        delivery_state="queued", response_state="awaiting_reply", platform="slack",
        platform_user_id="U1", outbox_id=11,
    )
    await record_outbox_result(sf, 11, provider_message_id="1234.5", error=None)
    assert (await _reload(sf, inv.id)).delivery_state == "delivered"


# ---------------------------------------------------------------------------
# Quiet hours, zones, exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quiet_hours_hold_the_opener_until_morning(sf, make_invitation):
    schedule = _schedule(quiet_hours={"start": "21:00", "end": "08:00"})
    inv = await make_invitation(delivery_state="queued", response_state="not_started")
    async with sf() as db:
        await db.execute(
            sa.update(ProgramInvitationRow).where(ProgramInvitationRow.id == inv.id)
            .values(program_id=schedule.program_id)
        )
        db.add(ProgramScheduleRow(
            program_id=schedule.program_id, org_id=inv.org_id, agent_id="a1",
            active=True, config=schedule.config,
        ))
        await db.commit()
    sent = []

    async def enqueue(row):
        sent.append(row.id)
        return 1

    lookup = _lookup({("whatsapp", inv.user_id): "40746148303"})
    # 04:30 UTC is 07:30 in Bucharest: still quiet.
    night = datetime.now(timezone.utc).replace(hour=4, minute=30)
    await send_queued_openers(sf, identity_lookup=lookup, now=night, enqueue=enqueue)
    assert sent == [] and (await _reload(sf, inv.id)).delivery_state == "queued"
    await send_queued_openers(sf, identity_lookup=lookup, now=night + timedelta(hours=1), enqueue=enqueue)
    assert sent == [inv.id]


@pytest.mark.asyncio
async def test_each_person_is_asked_at_the_steps_time_in_their_own_zone(sf):
    home, away = uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        for user, zone in ((home, None), (away, "America/New_York")):
            db.add(FollowupEnrollmentRow(
                org_id=ORG, agent_id=AGENT, user_id=user, protocol="onboarding",
                status="active", enrolled_by="agent", timezone=zone,
            ))
        await db.commit()
    schedule = _schedule(audience={"protocol": "onboarding"})
    lookup = _lookup({("whatsapp", home): "4070", ("whatsapp", away): "1555"})

    assert set(await _fire(sf, schedule, lookup)) == {home}
    # 08:00 in New York that day is 12:00 UTC.
    assert set(await _fire(sf, schedule, lookup, now=SLOT.replace(hour=12))) == {away}
    assert await journey.next_run_at(sf, schedule, SLOT) == SLOT.replace(hour=12)
    assert await journey.next_run_at(sf, schedule, SLOT.replace(hour=12)) == SLOT + timedelta(days=1)


@pytest.mark.asyncio
async def test_a_journey_ends_its_people_after_its_days(sf):
    done, ongoing = uuid.uuid4(), uuid.uuid4()
    schedule = _schedule(audience={"protocol": "onboarding"}, exit_after_days=180)
    async with sf() as db:
        db.add(ProgramScheduleRow(
            program_id=schedule.program_id, org_id=ORG, agent_id=AGENT,
            active=True, config=schedule.config,
        ))
        for user, start in ((done, date(2026, 4, 1)), (ongoing, date(2026, 10, 1))):
            db.add(FollowupEnrollmentRow(
                org_id=ORG, agent_id=AGENT, user_id=user, protocol="onboarding",
                status="active", enrolled_by="agent", start_date=start,
            ))
        await db.commit()
    assert await journey.finish_journeys(sf, now=SLOT) == 1
    [ended] = await followup_store.enrollments(sf, org_id=ORG, agent_id=AGENT, user_id=done)
    assert (ended.status, ended.status_reason) == ("ended", "Journey finished")
    [still] = await followup_store.enrollments(sf, org_id=ORG, agent_id=AGENT, user_id=ongoing)
    assert still.status == "active"


# ---------------------------------------------------------------------------
# Funnel and dry run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_funnel_counts_each_step_from_invited_to_answered(sf):
    from surogates.programs.funnel import step_funnel

    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lookup = _lookup({("whatsapp", a): "4070", ("whatsapp", b): "4071"})
    step = _schedule([a, b, c])
    rows = await _fire(sf, step, lookup)
    async with sf() as db:
        for user, delivery, response, read, outcome in (
            (a, "delivered", "completed", SLOT, "red"),
            (b, "failed", "not_started", None, None),
        ):
            row = await db.get(ProgramInvitationRow, rows[user].id)
            row.delivery_state, row.response_state, row.read_at, row.outcome = delivery, response, read, outcome
        await db.commit()
    other = uuid.uuid4()
    funnels = await step_funnel(
        sf, org_id=ORG, agent_id=AGENT, program_ids=[step.program_id, other],
        since=SLOT - timedelta(days=1),
    )
    f = funnels[step.program_id]
    assert (f["invited"], f["skipped"], f["sent"], f["delivered"], f["read"]) == (3, 1, 1, 1, 1)
    assert (f["replied"], f["completed"], f["failed"], f["no_reply"]) == (1, 1, 1, 0)
    assert f["skip_reasons"] == {"No reachable channel": 1}
    assert f["outcomes"] == {"red": 1} and f["channels"] == {"whatsapp": 2}
    assert funnels[other]["invited"] == 0


def test_the_dry_run_follows_cadence_window_and_zone():
    from surogates.programs.simulate import simulate

    steps = [
        {"id": "s1", "name": "Week one", "weekdays": ["mon", "wed", "fri"],
         "times_local": ["09:00"], "timezone": "Europe/Bucharest",
         "audience": {"protocol": "p", "day_from": 0, "day_to": 6}},
        {"id": "s2", "name": "Day ten", "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
         "times_local": ["10:00"], "timezone": "Europe/Bucharest",
         "audience": {"protocol": "p", "day_from": 10, "day_to": 10}},
    ]
    # Monday 2026-10-12 is day 0.
    events = simulate(steps, start_date=date(2026, 10, 12), timezone=None, days=14, today=date(2026, 10, 12))
    assert [(e["step_id"], e["at"][:16]) for e in events] == [
        ("s1", "2026-10-12T06:00"), ("s1", "2026-10-14T06:00"),
        ("s1", "2026-10-16T06:00"), ("s2", "2026-10-22T07:00"),
    ]
    # The person's own zone moves every slot to their local time.
    shifted = simulate(steps[:1], start_date=date(2026, 10, 12), timezone="America/New_York", days=1, today=date(2026, 10, 12))
    assert shifted == [{"at": "2026-10-12T13:00:00+00:00", "step_id": "s1", "name": "Week one"}]
