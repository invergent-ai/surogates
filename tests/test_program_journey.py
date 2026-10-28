"""A step inside a journey: its channel per person, fallback, quiet hours,
each person's zone and the journey's end."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

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
    # The rest are tried once each if the send fails; Telegram only for
    # someone who has written to the bot.
    assert rows[plain].fallback_platforms == ["slack"]


@pytest.mark.asyncio
async def test_a_preferred_channel_nobody_can_send_on_is_passed_over(sf):
    a = uuid.uuid4()
    await _enrol(sf, a, preferred_channel="slack")
    await _started_the_bot(sf)
    rows = await _fire(sf, _schedule(audience=AUDIENCE), _lookup({("telegram", a): "555"}))
    assert (rows[a].platform, rows[a].fallback_platforms) == ("telegram", None)


@pytest.mark.asyncio
async def test_reachable_only_where_the_agent_has_no_sender_is_skipped_visibly(sf):
    a = uuid.uuid4()
    schedule = _schedule([a])
    schedule.config["channel_identifier"] = None
    schedule.config["journey"]["senders"] = {"slack": "T1"}
    rows = await _fire(sf, schedule, _lookup({("whatsapp", a): "4070"}))
    assert (rows[a].delivery_state, rows[a].reason) == ("skipped", "No reachable channel")


def _key(row):
    return (row.platform, row.platform_user_id, row.channel_identifier, row.delivery_state, row.reason)


@pytest.mark.asyncio
async def test_a_whatsapp_only_person_is_asked_exactly_as_before_journeys(sf):
    ok, missing, withdrawn = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def lookup(org_id, platform, user_id):
        if platform != "whatsapp" or user_id == missing:
            return None
        ident = _granted("4070" if user_id == ok else "4071")
        if user_id == withdrawn:
            ident.platform_meta = {"contact_permission": {"status": "withdrawn"}}
        return ident

    with_journey = _schedule([ok, missing, withdrawn])
    without = _schedule([ok, missing, withdrawn])
    del without.config["journey"]
    after = await _fire(sf, with_journey, lookup)
    async with sf() as db:  # answered, so the second firing may ask again
        await db.execute(sa.update(ProgramInvitationRow).values(response_state="completed"))
        await db.commit()
    before = await _fire(sf, without, lookup)
    assert {u: _key(r) for u, r in after.items()} == {u: _key(r) for u, r in before.items()}
    assert _key(after[ok]) == ("whatsapp", "4070", "pn1", "queued", None)
    assert after[missing].reason == "No whatsapp identity"
    assert after[withdrawn].reason == "No permission to message"


@pytest.mark.asyncio
async def test_an_open_question_on_one_channel_is_not_asked_again_on_another(sf):
    a = uuid.uuid4()
    lookup = _lookup({("whatsapp", a): "4070", ("telegram", a): "555"})
    await _enrol(sf, a)
    await _started_the_bot(sf)
    first = await _fire(sf, _schedule(audience=AUDIENCE), lookup)
    async with sf() as db:
        row = await db.get(ProgramInvitationRow, first[a].id)
        row.response_state = "awaiting_reply"
        await db.commit()
    await _enrol_preference(sf, a, "telegram")
    again = (await _fire(sf, _schedule(audience=AUDIENCE), lookup, now=SLOT + timedelta(days=1)))[a]
    assert again.platform == "telegram"
    assert (again.delivery_state, again.reason) == ("skipped", "An earlier step is still waiting for an answer")


@pytest.mark.asyncio
async def test_two_steps_due_at_once_ask_a_person_one_question(sf):
    a = uuid.uuid4()
    lookup = _lookup({("whatsapp", a): "4070"})
    await _enrol(sf, a)
    first = (await _fire(sf, _schedule(audience=AUDIENCE), lookup))[a]
    second = (await _fire(sf, _schedule(audience=AUDIENCE), lookup))[a]
    assert first.delivery_state == "queued"
    assert (second.delivery_state, second.reason) == (
        "skipped", "An earlier step is still waiting for an answer",
    )


@pytest.mark.asyncio
async def test_silence_pauses_and_writing_resumes_on_the_channel_the_person_uses(sf):
    a = uuid.uuid4()
    lookup = _lookup({("telegram", a): "555"})
    await _enrol(sf, a)
    async with sf() as db:
        db.add(ChannelIdentity(
            org_id=ORG, user_id=a, platform="telegram", platform_user_id="555",
            platform_meta={"contact_permission": {"status": "granted"}},
        ))
        await db.commit()
    await _started_the_bot(sf)
    step = _schedule(audience=AUDIENCE)
    step.config["pause_after_missed"] = 1
    first = (await _fire(sf, step, lookup))[a]
    assert first.platform == "telegram"
    async with sf() as db:
        row = await db.get(ProgramInvitationRow, first.id)
        row.delivery_state, row.response_state = "delivered", "no_reply_by_deadline"
        await db.commit()
    paused = (await _fire(sf, step, lookup, now=SLOT + timedelta(days=1)))[a]
    assert paused.delivery_state == "skipped" and paused.reason.startswith("Paused")
    await followup_store.record_inbound(
        sf, org_id=ORG, platform="telegram", platform_user_id="555",
        channel_identifier="bot1", at=datetime.now(timezone.utc),  # after the rows were written
    )
    resumed = (await _fire(sf, step, lookup, now=SLOT + timedelta(days=2)))[a]
    assert (resumed.delivery_state, resumed.reason) == ("queued", None)


async def _enrol_preference(sf, user, channel):
    await followup_store.update_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol="onboarding",
        preferred_channel=channel,
    )


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


async def _started_the_bot(sf, at=SLOT - timedelta(days=30)):
    """The person wrote to the Telegram bot once, so it may write to them."""
    await followup_store.record_inbound(
        sf, org_id=ORG, platform="telegram", platform_user_id="555",
        channel_identifier="bot1", at=at,
    )


async def _fallback_invitation(sf, *, state, **extra):
    user = uuid.uuid4()
    await _started_the_bot(sf)
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
async def test_a_failed_status_without_a_reason_still_moves_on(sf):
    row = await _fallback_invitation(sf, state="accepted", provider_message_id="wamid.1")
    assert await apply_status_callback(sf, provider_message_id="wamid.1", status="failed", reason=None)
    moved = await _reload(sf, row.id)
    assert (moved.platform, moved.delivery_state, moved.response_state) == ("telegram", "queued", "not_started")


@pytest.mark.asyncio
async def test_a_failed_status_with_nowhere_left_frees_the_person(sf):
    row = await _fallback_invitation(
        sf, state="accepted", provider_message_id="wamid.2",
    )
    async with sf() as db:
        (await db.get(ProgramInvitationRow, row.id)).fallback_platforms = None
        await db.commit()
    await apply_status_callback(sf, provider_message_id="wamid.2", status="undelivered", reason=None)
    failed = await _reload(sf, row.id)
    assert (failed.delivery_state, failed.response_state) == ("undelivered", "not_started")


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
    # 07:30 in Bucharest is still quiet, 08:30 is not: at whatever offset from UTC the zone has today.
    night = datetime.now(ZoneInfo("Europe/Bucharest")).replace(hour=7, minute=30, second=0, microsecond=0)
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
async def test_a_journeys_own_zone_never_moves_a_steps_slots(sf):
    # A journey defaulting to UTC over Bucharest steps: the steps keep their
    # own zone, exactly as without a journey.
    a = uuid.uuid4()
    await _enrol(sf, a)
    with_journey = _schedule(audience=AUDIENCE, timezone="UTC")
    without = _schedule(audience=AUDIENCE)
    del without.config["journey"]
    lookup = _lookup({("whatsapp", a): "4070"})
    assert await journey.next_run_at(sf, with_journey, SLOT) == await journey.next_run_at(sf, without, SLOT)
    assert await journey.next_run_at(sf, with_journey, SLOT - timedelta(hours=1)) == SLOT
    assert set(await _fire(sf, with_journey, lookup)) == set(await _fire(sf, without, lookup)) == {a}


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
    assert f["skip_reasons"] == {"No whatsapp identity": 1}
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


def test_a_steps_own_sender_wins_on_its_channel():
    config = _schedule().config
    config["channel_identifier"] = "pn2"
    assert journey.senders_for(config) == {**SENDERS, "whatsapp": "pn2"}
    config["channel_identifier"] = ""
    assert journey.senders_for(config) == SENDERS


@pytest.mark.asyncio
async def test_telegram_counts_only_for_someone_who_wrote_to_the_bot(sf):
    a = uuid.uuid4()
    lookup = _lookup({("whatsapp", a): "4070", ("telegram", a): "555"})
    await _enrol(sf, a)
    await _enrol_preference(sf, a, "telegram")
    never_wrote = (await _fire(sf, _schedule(audience=AUDIENCE), lookup))[a]
    assert (never_wrote.platform, never_wrote.fallback_platforms) == ("whatsapp", None)
    await followup_store.record_inbound(
        sf, org_id=ORG, platform="telegram", platform_user_id="555",
        channel_identifier="bot1", at=SLOT - timedelta(days=1),
    )
    async with sf() as db:
        await db.execute(sa.update(ProgramInvitationRow).values(response_state="completed"))
        await db.commit()
    wrote = (await _fire(sf, _schedule(audience=AUDIENCE), lookup, now=SLOT + timedelta(days=1)))[a]
    assert wrote.platform == "telegram"


@pytest.mark.asyncio
async def test_a_fallback_skips_telegram_for_someone_who_never_wrote_to_the_bot(sf):
    from surogates.db.models import ChannelContactRow

    row = await _fallback_invitation(sf, state="failed")
    async with sf() as db:
        await db.execute(sa.delete(ChannelContactRow))
        await db.commit()
    assert await journey.advance_fallbacks(sf, now=SLOT) == 0
    left = await _reload(sf, row.id)
    assert left.platform == "whatsapp"
    assert [(a["platform"], a["state"]) for a in left.attempts][-1] == ("telegram", "skipped")


@pytest.mark.asyncio
async def test_someone_in_a_new_zone_brings_the_steps_next_run_forward(sf):
    schedule = _schedule(audience=AUDIENCE)
    # Fired at 08:00 Bucharest; the next Bucharest slot is tomorrow.
    tomorrow = SLOT + timedelta(days=1)
    async with sf() as db:
        db.add(ProgramScheduleRow(
            program_id=schedule.program_id, org_id=ORG, agent_id=AGENT,
            active=True, config=schedule.config, next_run_at=tomorrow,
        ))
        await db.commit()

    async def next_run():
        async with sf() as db:
            return (await db.execute(
                sa.select(ProgramScheduleRow.next_run_at)
                .where(ProgramScheduleRow.program_id == schedule.program_id)
            )).scalar_one()

    assert await journey.pull_forward_for_new_zones(sf, now=SLOT) == 0
    a = uuid.uuid4()
    await _enrol(sf, a, timezone="America/New_York")
    assert await journey.pull_forward_for_new_zones(sf, now=SLOT) == 1
    # 08:00 in New York the same day, well before Bucharest's next slot.
    assert as_utc(await next_run()) == datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    # Never later: a second pass leaves it.
    assert await journey.pull_forward_for_new_zones(sf, now=SLOT) == 0


def as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
