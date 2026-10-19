"""Outreach: text inside the provider window, a template outside it."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from surogates.db.models import EscalationRow, OutreachMessageRow
from surogates.followup import outreach, store
from surogates.session.events import EventType

ORG = uuid.uuid4()
AGENT = "a1"
SENDER = "pn1"
NOW = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self, fail_on_emit: bool = False):
        self.events: list[tuple] = []
        self.fail_on_emit = fail_on_emit

    async def emit_event(self, session_id, event_type, data, delivery_data=None, **_):
        if self.fail_on_emit:
            raise RuntimeError("database went away")
        self.events.append((session_id, event_type, data))
        self.delivered = delivery_data
        return len(self.events)

    async def emit_synthetic_user_message(self, session_id, *, content, synthetic, metadata):
        self.events.append((session_id, "synthetic", {"content": content, **metadata}))
        return len(self.events)


class _Delivery:
    def __init__(self):
        self.sent: list[tuple] = []

    async def enqueue(self, session_id, event_id, channel, destination, payload):
        self.sent.append((session_id, channel, destination, payload))
        return len(self.sent)


@pytest.fixture
def doctor_session(monkeypatch):
    session_id = uuid.uuid4()

    async def fake_dm_session(*_args, **kwargs):
        assert kwargs["session_key"] == "agent:whatsapp:dm:40711111111"
        assert kwargs["sender"] == SENDER
        return session_id

    monkeypatch.setattr(outreach, "dm_session", fake_dm_session)
    return session_id


def _lookup(permission="granted", missing=False):
    async def lookup(org_id, platform, user_id):
        if missing:
            return None
        return SimpleNamespace(
            platform_user_id="40711111111",
            platform_meta={"contact_permission": {"status": permission}},
        )
    return lookup


async def _queue(sf, user, text="Patient reports fainting", template=("alert", "ro")):
    return await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=user, platform="whatsapp",
        channel_identifier=SENDER, kind="alert", text=text,
        template_name=template[0] if template else None,
        template_language=template[1] if template else None,
    )


async def _row(sf, row_id):
    async with sf() as db:
        return await db.get(OutreachMessageRow, row_id)


async def _send(sf, session_store, delivery, lookup=None, now=NOW):
    return await outreach.send_queued_outreach(
        sf, session_store=session_store, redis=None, delivery_service=delivery,
        identity_lookup=lookup or _lookup(), now=now,
    )


async def _wrote(sf, at):
    await store.record_inbound(
        sf, org_id=ORG, platform="whatsapp", platform_user_id="40711111111",
        channel_identifier=SENDER, at=at,
    )


def test_window_boundaries():
    assert outreach.window_open(NOW - timedelta(hours=23), NOW)
    assert not outreach.window_open(NOW - timedelta(hours=23, minutes=55), NOW)
    assert not outreach.window_open(None, NOW)


@pytest.mark.asyncio
async def test_inside_the_window_the_text_goes_straight_out(sf, doctor_session):
    doctor = uuid.uuid4()
    await _wrote(sf, NOW - timedelta(hours=2))
    row = await _queue(sf, doctor)
    session_store, delivery = _Store(), _Delivery()

    assert await _send(sf, session_store, delivery) == 1

    [(session_id, event_type, data)] = session_store.events
    assert session_id == doctor_session
    assert event_type == EventType.LLM_RESPONSE
    assert data["message"]["content"] == "Patient reports fainting"
    assert delivery.sent == []  # the event itself is delivered by the store
    saved = await _row(sf, row.id)
    assert saved.state == "sent"
    assert saved.session_id == doctor_session


@pytest.mark.asyncio
async def test_outside_the_window_one_template_reopens_for_every_text(sf, doctor_session):
    doctor = uuid.uuid4()
    first = await _queue(sf, doctor, text="first")
    second = await _queue(sf, doctor, text="second")
    session_store, delivery = _Store(), _Delivery()

    assert await _send(sf, session_store, delivery) == 2

    [(session_id, channel, destination, payload)] = delivery.sent
    assert payload == {"template": {"name": "alert", "language": "ro"}}
    assert destination["wa_id"] == "40711111111"
    assert destination["phone_number_id"] == SENDER
    assert (await _row(sf, first.id)).state == "awaiting_reply"
    assert (await _row(sf, second.id)).state == "awaiting_reply"
    # Nothing written yet: the texts wait for the answer.
    assert all(e[1] != EventType.LLM_RESPONSE for e in session_store.events)


@pytest.mark.asyncio
async def test_the_reply_delivers_the_held_texts_in_order(sf, doctor_session):
    doctor = uuid.uuid4()
    first = await _queue(sf, doctor, text="first")
    second = await _queue(sf, doctor, text="second")
    await _send(sf, _Store(), _Delivery())

    reply_store = _Store()
    reply_session = uuid.uuid4()
    delivered = await outreach.deliver_pending(
        sf, reply_store, org_id=ORG, agent_id=AGENT, user_id=doctor,
        platform="whatsapp", channel_identifier=SENDER, session_id=reply_session,
    )

    assert delivered == 2
    assert [e[2]["message"]["content"] for e in reply_store.events] == ["first", "second"]
    assert all(e[0] == reply_session for e in reply_store.events)
    for row in (first, second):
        saved = await _row(sf, row.id)
        assert saved.state == "delivered"
        assert saved.delivered_at is not None
    # And never twice.
    assert await outreach.deliver_pending(
        sf, reply_store, org_id=ORG, agent_id=AGENT, user_id=doctor,
        platform="whatsapp", channel_identifier=SENDER, session_id=reply_session,
    ) == 0


@pytest.mark.asyncio
async def test_a_failed_write_gives_the_text_back(sf, doctor_session):
    doctor = uuid.uuid4()
    row = await _queue(sf, doctor)
    await _send(sf, _Store(), _Delivery())
    with pytest.raises(RuntimeError):
        await outreach.deliver_pending(
            sf, _Store(fail_on_emit=True), org_id=ORG, agent_id=AGENT,
            user_id=doctor, platform="whatsapp", channel_identifier=SENDER,
            session_id=uuid.uuid4(),
        )
    assert (await _row(sf, row.id)).state == "awaiting_reply"


@pytest.mark.asyncio
async def test_a_closed_window_without_a_template_fails_visibly(sf, doctor_session):
    row = await _queue(sf, uuid.uuid4(), template=None)
    await _send(sf, _Store(), _Delivery())
    saved = await _row(sf, row.id)
    assert saved.state == "failed"
    assert "window is closed" in saved.reason


@pytest.mark.parametrize("lookup, expected", [
    (_lookup(missing=True), "No whatsapp identity"),
    (_lookup(permission="withdrawn"), "No permission"),
])
@pytest.mark.asyncio
async def test_unreachable_recipients_fail_visibly(sf, doctor_session, lookup, expected):
    row = await _queue(sf, uuid.uuid4())
    await _send(sf, _Store(), _Delivery(), lookup=lookup)
    saved = await _row(sf, row.id)
    assert saved.state == "failed"
    assert expected in saved.reason


@pytest.mark.asyncio
async def test_a_burst_shares_a_template_but_a_later_alert_sends_its_own(sf, doctor_session):
    doctor = uuid.uuid4()
    await _queue(sf, doctor, text="first")
    delivery = _Delivery()
    await _send(sf, _Store(), delivery)
    await _queue(sf, doctor, text="five minutes later")
    await _send(sf, _Store(), delivery, now=NOW + timedelta(minutes=5))
    assert len(delivery.sent) == 1  # one notification for the burst
    await _queue(sf, doctor, text="an hour later, unrelated")
    await _send(sf, _Store(), delivery, now=NOW + timedelta(hours=1))
    # An urgent alert never waits behind an older, unanswered template.
    assert len(delivery.sent) == 2


async def _lease(sf, session_id, until):
    from surogates.db.models import SessionLease

    async with sf() as db:
        db.add(SessionLease(
            session_id=session_id, owner_id="worker-1",
            lease_token=uuid.uuid4(), expires_at=until.replace(tzinfo=None),
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_a_running_turn_is_never_written_into(sf, doctor_session):
    doctor = uuid.uuid4()
    await _wrote(sf, NOW - timedelta(hours=1))
    row = await _queue(sf, doctor)
    await _lease(sf, doctor_session, NOW + timedelta(minutes=2))
    store_ = _Store()
    assert await _send(sf, store_, _Delivery()) == 0
    assert store_.events == []
    assert (await _row(sf, row.id)).state == "queued"
    # The turn ends; the next tick writes it.
    assert await _send(sf, store_, _Delivery(), now=NOW + timedelta(minutes=3)) == 1
    assert (await _row(sf, row.id)).state == "sent"


@pytest.mark.asyncio
async def test_a_held_text_reaches_someone_who_answered_during_a_turn(sf, doctor_session):
    doctor = uuid.uuid4()
    row = await _queue(sf, doctor)
    await _send(sf, _Store(), _Delivery())  # template out, text held
    await _lease(sf, doctor_session, NOW + timedelta(minutes=2))
    await _wrote(sf, NOW + timedelta(minutes=1))
    skipped = await outreach.deliver_pending(
        sf, _Store(), org_id=ORG, agent_id=AGENT, user_id=doctor,
        platform="whatsapp", channel_identifier=SENDER,
        session_id=doctor_session, now=NOW + timedelta(minutes=1),
    )
    assert skipped == 0  # the worker is mid-turn
    store_ = _Store()
    assert await _send(sf, store_, _Delivery(), now=NOW + timedelta(minutes=3)) == 1
    assert store_.events[0][2]["message"]["content"] == "Patient reports fainting"
    assert (await _row(sf, row.id)).state == "delivered"


@pytest.mark.asyncio
async def test_a_failed_template_fails_its_texts_at_once(sf, doctor_session):
    doctor = uuid.uuid4()
    first = await _queue(sf, doctor, text="first")
    await _send(sf, _Store(), _Delivery())
    second = await _queue(sf, doctor, text="second")
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(minutes=2))
    opener = await _row(sf, first.id)
    assert opener.outbox_id == 1
    assert await outreach.record_outreach_delivery(
        sf, opener.outbox_id, provider_message_id=None,
        error="(#132001) Template name does not exist in the translation",
    )
    for row in (first, second):
        saved = await _row(sf, row.id)
        assert saved.state == "failed"
        assert "132001" in saved.reason


@pytest.mark.asyncio
async def test_a_late_provider_failure_is_recorded_too(sf, doctor_session):
    from surogates.programs.delivery import _apply_status_anywhere

    doctor = uuid.uuid4()
    row = await _queue(sf, doctor)
    await _send(sf, _Store(), _Delivery())
    opener = await _row(sf, row.id)
    assert await outreach.record_outreach_delivery(
        sf, opener.outbox_id, provider_message_id="wamid.T1", error=None,
    )
    assert (await _row(sf, row.id)).provider_message_id == "wamid.T1"
    # The status webhook finds it among the unprompted templates it knows.
    assert await _apply_status_anywhere(
        sf, provider_message_id="wamid.T1", status="failed",
        reason="131026 Message undeliverable",
    )
    saved = await _row(sf, row.id)
    assert saved.state == "failed"
    assert "131026" in saved.reason


@pytest.mark.asyncio
async def test_unanswered_templates_expire(sf, doctor_session):
    row = await _queue(sf, uuid.uuid4())
    await _send(sf, _Store(), _Delivery())
    expired = await outreach._expire_unanswered(sf, now=NOW + timedelta(hours=73))
    assert expired == 1
    saved = await _row(sf, row.id)
    assert saved.state == "failed"
    assert "did not answer" in saved.reason


@pytest.mark.asyncio
async def test_queue_refuses_channels_without_outreach(sf):
    with pytest.raises(ValueError):
        await store.queue_outreach(
            sf, org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(),
            platform="website", channel_identifier="A1", kind="alert",
            text="x", template_name=None, template_language=None,
        )


@pytest.mark.asyncio
async def test_only_queued_rows_are_picked_up(sf, doctor_session):
    doctor = uuid.uuid4()
    await _wrote(sf, NOW - timedelta(hours=1))
    row = await _queue(sf, doctor)
    async with sf() as db:
        await db.execute(
            sa.update(OutreachMessageRow)
            .where(OutreachMessageRow.id == row.id)
            .values(state="delivered")
        )
        await db.commit()
    assert await _send(sf, _Store(), _Delivery()) == 0


@pytest.mark.asyncio
async def test_an_alert_never_rides_on_a_routine_notice(sf, doctor_session):
    doctor = uuid.uuid4()
    await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=doctor, platform="whatsapp",
        channel_identifier=SENDER, kind="enrolment", text="New patient enrolled",
        template_name="alert", template_language="ro",
    )
    delivery = _Delivery()
    await _send(sf, _Store(), delivery)
    await _queue(sf, doctor, text="Patient fainted")
    await _send(sf, _Store(), delivery, now=NOW + timedelta(minutes=5))
    assert len(delivery.sent) == 2  # the alert pinged on its own


@pytest.mark.asyncio
async def test_a_template_reported_before_its_outbox_id_is_stamped_still_fails_its_texts(sf):
    """The dispatcher can send (or fail) a reopening template before _reopen
    has written outbox_id on the row.  The report must still find it."""
    from surogates.db.models import DeliveryOutbox, Event
    from surogates.session.events import OUTREACH_OPENER_MARKER

    doctor = uuid.uuid4()
    row = await _queue(sf, doctor)
    async with sf() as db:
        saved = await db.get(OutreachMessageRow, row.id)
        saved.state, saved.sent_at = "awaiting_reply", NOW.replace(tzinfo=None)
        session_id = uuid.uuid4()
        db.add(Event(id=901, session_id=session_id, type=EventType.USER_MESSAGE.value, data={
            "synthetic": OUTREACH_OPENER_MARKER, "outreach_id": str(row.id),
        }))
        db.add(DeliveryOutbox(
            id=77, session_id=session_id, event_id=901, channel="whatsapp",
            destination={}, payload={}, status="pending",
        ))
        await db.commit()

    assert await outreach.record_outreach_delivery(
        sf, 77, provider_message_id=None, error="template paused by Meta",
    )
    failed = await _row(sf, row.id)
    assert failed.state == "failed"
    assert failed.outbox_id == 77


@pytest.mark.asyncio
async def test_a_failed_template_spares_texts_behind_another_template(sf, doctor_session):
    """An operator's template and an alert's own template go out minutes
    apart.  The operator's failing must not fail the alert."""
    doctor = uuid.uuid4()
    routine = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=doctor, platform="whatsapp",
        channel_identifier=SENDER, kind="operator", text="A note from the practice",
        template_name="message", template_language="ro",
    )
    delivery = _Delivery()
    await _send(sf, _Store(), delivery)
    alert = await _queue(sf, doctor, text="Patient fainted")
    await _send(sf, _Store(), delivery, now=NOW + timedelta(minutes=5))
    assert len(delivery.sent) == 2
    failed_outbox = (await _row(sf, routine.id)).outbox_id
    assert await outreach.record_outreach_delivery(
        sf, failed_outbox, provider_message_id=None, error="template paused",
    )
    assert (await _row(sf, routine.id)).state == "failed"
    assert (await _row(sf, alert.id)).state == "awaiting_reply"


# --- what became of a team member's message ---------------------------------


async def _team_message(sf, *, hours=None):
    """A practice with one team member, and his message to an enrolled person."""
    from datetime import date

    from surogates.db.models import ChannelIdentity, User

    member, person = uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        db.add(User(id=person, org_id=ORG, email=f"{person}@x", display_name="Ion"))
        db.add(ChannelIdentity(
            org_id=ORG, user_id=person, platform="whatsapp",
            platform_user_id="40711111111", platform_meta={},
        ))
        await db.commit()
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=person, protocol="p",
        start_date=date(2026, 10, 1), phase=None, enrolled_by="agent",
        patient_name="Ion Pop",
    )
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, channel_identifier=SENDER,
        alert_recipient_user_ids=[member], alert_template_name="alert",
        alert_template_language="ro", operator_unanswered_hours=hours,
    )
    row = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=person, platform="whatsapp",
        channel_identifier=SENDER, kind="operator", text="Come tomorrow",
        template_name="msg", template_language="ro", subject_user_id=person,
        created_by=f"team:{member}",
    )
    await _send(sf, _Store(), _Delivery())
    assert (await _row(sf, row.id)).state == "awaiting_reply"
    return member, row


async def _notices(sf):
    async with sf() as db:
        return (await db.execute(
            sa.select(OutreachMessageRow).where(OutreachMessageRow.kind == "notice")
        )).scalars().all()


@pytest.mark.asyncio
async def test_the_requester_hears_once_that_a_held_message_arrived(sf, doctor_session):
    member, row = await _team_message(sf)
    at = NOW + timedelta(hours=2)
    await outreach.deliver_pending(
        sf, _Store(), org_id=ORG, agent_id=AGENT, user_id=row.user_id,
        platform="whatsapp", channel_identifier=SENDER, session_id=uuid.uuid4(), now=at,
    )
    await _send(sf, _Store(), _Delivery(), now=at)
    await _send(sf, _Store(), _Delivery(), now=at)

    [notice] = await _notices(sf)
    assert notice.user_id == member
    # No Program names a timezone, so the time is UTC.
    assert notice.text == "✅ Ion Pop received your message (04:00)."
    assert notice.template_name == "alert"


@pytest.mark.asyncio
async def test_an_unanswered_message_expires_after_a_day_and_the_requester_is_told(sf, doctor_session):
    member, row = await _team_message(sf)
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(hours=23))
    assert (await _row(sf, row.id)).state == "awaiting_reply"
    assert await _notices(sf) == []

    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(hours=24, minutes=1))
    assert (await _row(sf, row.id)).state == "failed"
    [notice] = await _notices(sf)
    assert notice.user_id == member
    assert notice.text == (
        "⚠️ Ion Pop did not receive your message: no answer to the opening "
        "template. Call them? +40 711 111 111"
    )


@pytest.mark.asyncio
async def test_the_agents_setting_moves_the_expiry_but_not_other_texts(sf, doctor_session):
    _, row = await _team_message(sf, hours=6)
    alert = await _queue(sf, uuid.uuid4())
    await _send(sf, _Store(), _Delivery())
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(hours=7))
    assert (await _row(sf, row.id)).state == "failed"
    assert "6 hours" in (await _row(sf, row.id)).reason
    assert (await _row(sf, alert.id)).state == "awaiting_reply"


# --- red alerts nobody opened -----------------------------------------------


async def _red_alert(sf, team, *, minutes=None, invitation_id="-", escalation_id=None):
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, channel_identifier=SENDER,
        alert_recipient_user_ids=team, alert_template_name="alert",
        alert_template_language="ro", red_alert_reminder_minutes=minutes,
    )
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    await store.notify_care_team(
        sf, settings, org_id=ORG, agent_id=AGENT, kind="alert", text="🔴 Faint",
        created_by="agent", severity="red",
        dedupe_prefix=f"escalation:{invitation_id}:{(escalation_id or uuid.uuid4()).hex}",
    )


async def _alerts(sf, created_by):
    async with sf() as db:
        return (await db.execute(
            sa.select(OutreachMessageRow).where(OutreachMessageRow.created_by == created_by)
        )).scalars().all()


async def _at(sf, minutes):
    """Pretend every alert was raised *minutes* before NOW."""
    async with sf() as db:
        await db.execute(
            sa.update(OutreachMessageRow).values(created_at=NOW - timedelta(minutes=minutes))
        )
        await db.commit()


@pytest.mark.asyncio
async def test_an_unopened_red_alert_is_sent_once_more_to_the_whole_team(sf, doctor_session):
    first, second = uuid.uuid4(), uuid.uuid4()
    await _red_alert(sf, [first, second])
    await _send(sf, _Store(), _Delivery(), now=NOW - timedelta(minutes=31))
    await _at(sf, 29)
    await _send(sf, _Store(), _Delivery())
    assert await _alerts(sf, "reminder") == []

    await _at(sf, 31)
    delivery = _Delivery()
    await _send(sf, _Store(), delivery)
    reminders = await _alerts(sf, "reminder")
    assert {r.user_id for r in reminders} == {first, second}
    assert all(r.severity == "red" for r in reminders)
    assert reminders[0].text.startswith("🔴 Reminder, not yet opened after 30 minutes")
    # Each reminder reopens with a fresh template rather than riding on the first.
    assert [p for *_, p in delivery.sent] == [{"template": {"name": "alert", "language": "ro"}}] * 2
    assert all(r.reminded_at is not None for r in await _alerts(sf, "agent"))

    for later in (5, 60, 600):
        await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(minutes=later))
    assert len(await _alerts(sf, "reminder")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("handled", [{"owner": "Ana"}, {"resolved_at": NOW}])
async def test_a_taken_or_resolved_escalation_is_not_reminded(sf, doctor_session, handled, monkeypatch):
    taken, open_ = uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        for escalation_id, extra in ((taken, handled), (open_, {})):
            db.add(EscalationRow(
                id=escalation_id, org_id=ORG, agent_id=AGENT, kind="alert",
                severity="red", reason="Faint", **extra,
            ))
        await db.commit()
    await _red_alert(sf, [uuid.uuid4()], escalation_id=taken)
    await _red_alert(sf, [uuid.uuid4()], escalation_id=open_)
    await _send(sf, _Store(), _Delivery(), now=NOW - timedelta(minutes=40))
    await _at(sf, 40)
    # A batch of one: the handled, older alert must not take the newer's place.
    async with sf() as db:
        await db.execute(
            sa.update(OutreachMessageRow)
            .where(OutreachMessageRow.dedupe_key.like(f"%{taken.hex}%"))
            .values(created_at=NOW - timedelta(minutes=41))
        )
        await db.commit()
    monkeypatch.setattr(outreach, "BATCH", 1)
    await _send(sf, _Store(), _Delivery())
    [reminder] = await _alerts(sf, "reminder")
    assert open_.hex in reminder.dedupe_key


@pytest.mark.asyncio
async def test_an_opened_or_yellow_alert_is_not_reminded(sf, doctor_session):
    member = uuid.uuid4()
    await _wrote(sf, NOW - timedelta(hours=1))  # window open: the alert reads as text
    await _red_alert(sf, [member])
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    await store.notify_care_team(
        sf, settings, org_id=ORG, agent_id=AGENT, kind="alert", text="Mild",
        created_by="agent", severity="yellow", dedupe_prefix=f"escalation:-:{uuid.uuid4().hex}",
    )
    await _send(sf, _Store(), _Delivery(), now=NOW - timedelta(minutes=40))
    [red] = [r for r in await _alerts(sf, "agent") if r.severity == "red"]
    assert red.state == "sent"
    await _read(sf, red)
    await _at(sf, 40)
    await _send(sf, _Store(), _Delivery())
    assert await _alerts(sf, "reminder") == []


async def _read(sf, row, wamid="wamid.text", column="text_message_id"):
    """WhatsApp reports the text of *row* (or its template) read."""
    async with sf() as db:
        saved = await db.get(OutreachMessageRow, row.id)
        setattr(saved, column, wamid)
        await db.commit()
    assert await outreach.apply_outreach_status(
        sf, provider_message_id=wamid, status="read", reason=None,
    )


@pytest.mark.asyncio
async def test_a_red_alert_sent_as_text_but_unread_is_reminded(sf, doctor_session):
    await _wrote(sf, NOW - timedelta(hours=1))
    await _red_alert(sf, [uuid.uuid4()])
    await _send(sf, _Store(), _Delivery(), now=NOW - timedelta(minutes=40))
    assert [r.state for r in await _alerts(sf, "agent")] == ["sent"]
    await _at(sf, 40)
    await _send(sf, _Store(), _Delivery())
    assert len(await _alerts(sf, "reminder")) == 1


@pytest.mark.asyncio
async def test_a_template_read_alone_is_not_a_read_alert(sf, doctor_session):
    await _red_alert(sf, [uuid.uuid4()])
    await _send(sf, _Store(), _Delivery(), now=NOW - timedelta(minutes=40))
    [held] = await _alerts(sf, "agent")
    assert held.state == "awaiting_reply"
    await _read(sf, held, "wamid.template", "provider_message_id")
    assert (await _row(sf, held.id)).read_at is None


@pytest.mark.asyncio
async def test_a_held_recipient_gets_the_reminder_instead_of_the_alert_twice(sf, doctor_session):
    member = uuid.uuid4()
    await _red_alert(sf, [member])
    await _send(sf, _Store(), _Delivery(), now=NOW - timedelta(minutes=40))
    await _at(sf, 40)
    await _send(sf, _Store(), _Delivery())
    [original] = await _alerts(sf, "agent")
    assert original.state == "replaced"

    session_store = _Store()
    await outreach.deliver_pending(
        sf, session_store, org_id=ORG, agent_id=AGENT, user_id=member,
        platform="whatsapp", channel_identifier=SENDER, session_id=uuid.uuid4(),
        now=NOW + timedelta(minutes=1),
    )
    [(_, _, data)] = session_store.events
    assert data["message"]["content"].startswith("🔴 Reminder")


@pytest.mark.asyncio
async def test_the_outbox_result_gives_an_outreach_text_its_provider_id(sf):
    from surogates.db.models import DeliveryOutbox, Event

    row = await _queue(sf, uuid.uuid4())
    session_id = uuid.uuid4()
    async with sf() as db:
        db.add(Event(id=902, session_id=session_id, type=EventType.LLM_RESPONSE.value, data={
            "synthetic": "outreach", "outreach_id": str(row.id),
        }))
        db.add(DeliveryOutbox(
            id=78, session_id=session_id, event_id=902, channel="whatsapp",
            destination={}, payload={"content": "x"}, status="pending",
        ))
        await db.commit()
    assert await outreach.record_outreach_text(sf, 78, provider_message_id="wamid.x")
    assert (await _row(sf, row.id)).text_message_id == "wamid.x"


@pytest.mark.asyncio
async def test_the_text_id_does_not_replace_the_templates(sf):
    from surogates.db.models import DeliveryOutbox, Event

    row = await _queue(sf, uuid.uuid4())
    session_id = uuid.uuid4()
    async with sf() as db:
        saved = await db.get(OutreachMessageRow, row.id)
        saved.state, saved.provider_message_id = "awaiting_reply", "wamid.template"
        db.add(Event(id=903, session_id=session_id, type=EventType.LLM_RESPONSE.value, data={
            "synthetic": "outreach", "outreach_id": str(row.id),
        }))
        db.add(DeliveryOutbox(
            id=79, session_id=session_id, event_id=903, channel="whatsapp",
            destination={}, payload={"content": "x"}, status="pending",
        ))
        await db.commit()
    assert await outreach.record_outreach_text(sf, 79, provider_message_id="wamid.text")

    assert await outreach.apply_outreach_status(
        sf, provider_message_id="wamid.template", status="failed", reason="131026",
    )
    saved = await _row(sf, row.id)
    assert (saved.provider_message_id, saved.text_message_id) == ("wamid.template", "wamid.text")
    assert saved.state == "failed"


@pytest.mark.asyncio
async def test_the_reminder_wait_is_the_agents_setting_and_marks_the_check_in(sf, doctor_session):
    from surogates.db.models import ProgramInvitationRow

    invitation_id = uuid.uuid4()
    async with sf() as db:
        db.add(ProgramInvitationRow(
            id=invitation_id, org_id=ORG, agent_id=AGENT, program_id=uuid.uuid4(),
            occurrence_id=uuid.uuid4(), user_id=uuid.uuid4(), platform="whatsapp",
            platform_user_id="40700000001", escalation_state="raised",
        ))
        await db.commit()
    await _red_alert(sf, [uuid.uuid4()], minutes=60, invitation_id=invitation_id)
    await _send(sf, _Store(), _Delivery(), now=NOW - timedelta(minutes=61))
    await _at(sf, 45)
    await _send(sf, _Store(), _Delivery())
    assert await _alerts(sf, "reminder") == []
    await _at(sf, 61)
    await _send(sf, _Store(), _Delivery())
    assert len(await _alerts(sf, "reminder")) == 1
    async with sf() as db:
        invitation = await db.get(ProgramInvitationRow, invitation_id)
    # Run counts read escalation_state == "raised"; the reminder must not move it.
    assert invitation.escalation_state == "raised"
    assert invitation.reminder_state == "reminded"


# --- failures reaching a person ---------------------------------------------


async def _failed_opener(sf, user_id, *, error, at=NOW, state="failed", fallbacks=None):
    from surogates.db.models import ProgramInvitationRow

    async with sf() as db:
        db.add(ProgramInvitationRow(
            org_id=ORG, agent_id=AGENT, program_id=uuid.uuid4(),
            occurrence_id=uuid.uuid4(), user_id=user_id, platform="whatsapp",
            platform_user_id="40711111111", delivery_state=state,
            delivery_error=error if state != "canceled" else None,
            reason=error if state == "canceled" else None, created_at=at,
            fallback_platforms=fallbacks,
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_a_failed_program_opener_tells_the_team_once_per_person_per_day(sf, doctor_session):
    member, row = await _team_message(sf)
    person = row.user_id
    await _failed_opener(sf, person, error="graph error 132015 (template paused)")
    await _failed_opener(sf, person, error="graph error 131047 (window closed)",
                         at=NOW + timedelta(seconds=30))
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(minutes=1))
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(minutes=2))

    [notice] = await _notices(sf)
    assert notice.user_id == member
    assert notice.subject_user_id == person
    assert notice.text == (
        "⚠️ A message to Ion Pop did not arrive: WhatsApp refused the "
        "template. Call them? +40 711 111 111"
    )

    await _failed_opener(sf, person, error="Permission withdrawn before the opener was sent",
                         at=NOW + timedelta(days=1), state="canceled")
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(days=1, minutes=1))
    texts = [n.text for n in await _notices(sf) if n.text.startswith("⚠️ A message")]
    assert len(texts) == 2
    assert "no permission to message them" in texts[0] + texts[1]


@pytest.mark.asyncio
async def test_an_opener_with_another_channel_to_try_is_not_reported_yet(sf, doctor_session):
    _, row = await _team_message(sf)
    await _failed_opener(sf, row.user_id, error="Never started the bot", fallbacks=["whatsapp"])
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(minutes=1))
    assert [n for n in await _notices(sf) if n.text.startswith("⚠️ A message")] == []


@pytest.mark.asyncio
async def test_a_failed_welcome_is_surfaced_but_a_team_message_is_reported_to_its_sender(sf, doctor_session):
    member, row = await _team_message(sf)
    welcome = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=row.user_id, platform="whatsapp",
        channel_identifier=SENDER, kind="welcome", text="Welcome",
        template_name=None, template_language=None, created_by="purchase",
    )
    await _send(sf, _Store(), _Delivery())
    assert (await _row(sf, welcome.id)).state == "failed"
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(hours=25))

    notices = sorted(n.text for n in await _notices(sf))
    assert len(notices) == 2
    assert notices[0].startswith("⚠️ A message to Ion Pop did not arrive: their 24h window")
    assert notices[1].startswith("⚠️ Ion Pop did not receive your message: no answer")


@pytest.mark.asyncio
async def test_a_failed_welcome_a_team_member_asked_for_is_reported_to_them(sf, doctor_session):
    member, row = await _team_message(sf)
    welcome = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=row.user_id, platform="whatsapp",
        channel_identifier=SENDER, kind="welcome", text="Welcome",
        template_name=None, template_language=None, created_by=f"team:{member}",
    )
    await _send(sf, _Store(), _Delivery())
    assert (await _row(sf, welcome.id)).state == "failed"
    await _send(sf, _Store(), _Delivery())

    [notice] = await _notices(sf)
    assert notice.user_id == member
    assert notice.text.startswith("⚠️ Ion Pop did not receive your message: their 24h window")


@pytest.mark.asyncio
async def test_failures_are_reported_once_newest_first_and_only_where_a_team_listens(
    sf, doctor_session, monkeypatch,
):
    from surogates.db.models import ProgramInvitationRow

    member, row = await _team_message(sf)
    older, newer = uuid.uuid4(), row.user_id
    await _failed_opener(sf, older, error="graph error 132015", at=NOW - timedelta(hours=2))
    await _failed_opener(sf, newer, error="graph error 132015", at=NOW - timedelta(hours=1))
    async with sf() as db:
        db.add(ProgramInvitationRow(
            org_id=ORG, agent_id="no-team", program_id=uuid.uuid4(),
            occurrence_id=uuid.uuid4(), user_id=uuid.uuid4(), platform="whatsapp",
            platform_user_id="40799999999", delivery_state="failed",
            delivery_error="graph error 132015", created_at=NOW,
        ))
        await db.commit()
    monkeypatch.setattr(outreach, "BATCH", 1)

    assert await outreach._surface_person_failures(sf, now=NOW) == 1
    [notice] = await _notices(sf)
    assert notice.subject_user_id == newer
    assert await outreach._surface_person_failures(sf, now=NOW) == 1
    assert {n.subject_user_id for n in await _notices(sf)} == {newer, older}
    assert await outreach._surface_person_failures(sf, now=NOW) == 0

    async with sf() as db:
        rows = (await db.execute(sa.select(ProgramInvitationRow))).scalars().all()
    assert {r.agent_id for r in rows if r.reported_at is None} == {"no-team"}


@pytest.mark.asyncio
async def test_an_outcome_is_marked_reported_and_never_read_again(sf, doctor_session):
    member, row = await _team_message(sf)
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(hours=25))
    assert (await _row(sf, row.id)).reported_at is not None
    assert await outreach._report_outcomes(sf, now=NOW + timedelta(hours=26)) == 0
    assert len(await _notices(sf)) == 1


@pytest.mark.asyncio
async def test_messages_to_one_person_expiring_together_are_reported_once(sf, doctor_session):
    member, row = await _team_message(sf)
    second = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=row.user_id, platform="whatsapp",
        channel_identifier=SENDER, kind="operator", text="And bring the results",
        template_name="msg", template_language="ro", subject_user_id=row.user_id,
        created_by=f"team:{member}",
    )
    await _send(sf, _Store(), _Delivery())
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(hours=25))

    assert {(await _row(sf, r)).state for r in (row.id, second.id)} == {"failed"}
    [notice] = await _notices(sf)
    assert notice.text.startswith("⚠️ Ion Pop did not receive your message: no answer")
    assert (await _row(sf, second.id)).reported_at is not None


@pytest.mark.asyncio
async def test_a_reminder_that_could_not_be_queued_is_retried(sf, doctor_session, monkeypatch):
    await _red_alert(sf, [uuid.uuid4()])
    await _send(sf, _Store(), _Delivery(), now=NOW - timedelta(minutes=31))
    await _at(sf, 31)
    real = store.notify_team

    async def down(*args, **kwargs):
        if kwargs.get("created_by") == "reminder":
            raise RuntimeError("database went away")
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, "notify_team", down)
    await _send(sf, _Store(), _Delivery())
    assert await _alerts(sf, "reminder") == []
    assert all(r.reminded_at is None for r in await _alerts(sf, "agent"))

    monkeypatch.setattr(store, "notify_team", real)
    await _send(sf, _Store(), _Delivery())
    assert len(await _alerts(sf, "reminder")) == 1
    assert all(r.reminded_at is not None for r in await _alerts(sf, "agent"))


@pytest.mark.asyncio
async def test_a_failing_bookkeeping_pass_never_skips_the_send_pass(sf, doctor_session, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    for name in ("_expire_unanswered", "_report_outcomes", "_remind_unseen_red_alerts",
                 "_surface_person_failures"):
        monkeypatch.setattr(outreach, name, broken)
    await _wrote(sf, NOW - timedelta(hours=2))
    row = await _queue(sf, uuid.uuid4())
    assert await _send(sf, _Store(), _Delivery()) == 1
    assert (await _row(sf, row.id)).state == "sent"


@pytest.mark.asyncio
async def test_a_studio_message_keeps_the_general_wait(sf, doctor_session):
    await _team_message(sf, hours=6)
    studio = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(), platform="whatsapp",
        channel_identifier=SENDER, kind="operator", text="From Studio",
        template_name="msg", template_language="ro", created_by=None,
    )
    await _send(sf, _Store(), _Delivery())
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(hours=25))
    assert (await _row(sf, studio.id)).state == "awaiting_reply"
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(hours=73))
    assert "72 hours" in (await _row(sf, studio.id)).reason


@pytest.mark.asyncio
async def test_an_answer_after_expiry_is_told_kindly_and_the_requester_hears(sf, doctor_session):
    member, row = await _team_message(sf)
    await _send(sf, _Store(), _Delivery(), now=NOW + timedelta(hours=25))
    assert (await _row(sf, row.id)).state == "failed"

    later = NOW + timedelta(hours=30)
    person_store = _Store()
    for _ in range(2):
        await outreach.deliver_pending(
            sf, person_store, org_id=ORG, agent_id=AGENT, user_id=row.user_id,
            platform="whatsapp", channel_identifier=SENDER, session_id=uuid.uuid4(), now=later,
        )
    [(_, event_type, data)] = person_store.events
    assert event_type == EventType.LLM_RESPONSE
    assert "expired" in data["message"]["content"]
    assert "Come tomorrow" not in data["message"]["content"]
    assert (await _row(sf, row.id)).state == "answered_late"
    late = [n for n in await _notices(sf) if n.text.startswith("↩️")]
    assert [n.user_id for n in late] == [member]
    assert late[0].text.startswith("↩️ Ion Pop answered after your message expired")



@pytest.mark.asyncio
async def test_the_log_keeps_a_number_masked_and_only_the_delivery_has_it_whole(sf, doctor_session):
    await _wrote(sf, NOW - timedelta(hours=2))
    await _queue(sf, uuid.uuid4(), text="Faint\n\nAbout: Ion Pop · +40 711 111 111")
    session_store = _Store()
    await _send(sf, session_store, _Delivery())
    [(_, _, data)] = session_store.events
    assert data["message"]["content"] == "Faint\n\nAbout: Ion Pop · +40…111"
    assert data["platform_note"] == outreach.MASKED_NOTE
    assert session_store.delivered["message"]["content"] == "Faint\n\nAbout: Ion Pop · +40 711 111 111"
    # The channel takes the text alone: the model's note never goes out.
    from surogates.session.store import _build_channel_payload

    assert _build_channel_payload(EventType.LLM_RESPONSE, session_store.delivered, "whatsapp") == {
        "content": "Faint\n\nAbout: Ion Pop · +40 711 111 111", "intermediate": False,
    }


@pytest.mark.asyncio
async def test_an_escalation_alert_reminds_the_model_to_close_it_and_delivers_unchanged(sf, doctor_session):
    await _wrote(sf, NOW - timedelta(hours=2))
    await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(), platform="whatsapp",
        channel_identifier=SENDER, kind="alert", text="Faint", template_name="alert", template_language="ro",
        dedupe_key=f"escalation:-:{uuid.uuid4().hex}:x",
    )
    session_store = _Store()
    await _send(sf, session_store, _Delivery())
    [(_, _, data)] = session_store.events
    assert data["message"]["content"] == "Faint"
    assert data["platform_note"] == outreach.ESCALATION_NOTE
    assert session_store.delivered["message"]["content"] == "Faint"



def test_every_reason_reads_in_the_agents_own_words():
    texts = {
        "why_window_closed": "fereastra de 24h s-a închis",
        "why_no_permission": "nu avem permisiunea",
        "why_template_failed": "WhatsApp a refuzat șablonul",
        "why_expired": "nu a răspuns la șablon",
        "why_no_template": "nu e ales niciun șablon",
    }
    for raw, key in (
        ("graph error 131047", "why_window_closed"),
        ("No permission to message the recipient", "why_no_permission"),
        ("The reopening template was not sent: 132015 paused", "why_template_failed"),
        ("The recipient did not answer the template within 24 hours", "why_expired"),
        ("window is closed and no approved template is configured", "why_no_template"),
    ):
        assert outreach.plain_reason(raw, texts) == texts[key]
        assert outreach.plain_reason(raw) == outreach.DEFAULT_TEXTS[key]


class _SlackClient:
    def __init__(self):
        self.opened = []

    async def conversations_open(self, *, users):
        self.opened.append(users)
        return {"channel": {"id": "D42"}}


@pytest.mark.asyncio
async def test_slack_outreach_goes_straight_into_the_direct_message(sf, monkeypatch):
    # No service window and no template: the text is written into the DM the
    # member's reply lands in, whenever they last wrote.
    keys = []

    async def fake_dm_session(*_args, **kwargs):
        keys.append((kwargs["session_key"], kwargs["channel_id"], kwargs["sender"]))
        return uuid.uuid4()

    monkeypatch.setattr(outreach, "dm_session", fake_dm_session)
    row = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(), platform="slack",
        channel_identifier="A1", kind="alert", text="Needs a call",
        template_name=None, template_language=None,
    )

    async def lookup(org_id, platform, user_id):
        assert platform == "slack"
        return SimpleNamespace(
            platform_user_id="U7", platform_meta={"contact_permission": {"status": "granted"}},
        )

    async def creds(kind, identifier, org_id):
        return {"bot_token": "xoxb"}

    client, session_store, delivery = _SlackClient(), _Store(), _Delivery()
    moved = await outreach.send_queued_outreach(
        sf, session_store=session_store, redis=None, delivery_service=delivery,
        identity_lookup=lookup, now=NOW, credentials_for=creds,
        slack_client_factory=lambda token: client,
    )
    assert moved == 1
    assert client.opened == ["U7"]
    assert keys == [("agent:slack:dm:D42", "D42", "A1")]
    assert delivery.sent == []
    [(_, event_type, data)] = session_store.events
    assert event_type == EventType.LLM_RESPONSE and data["message"]["content"] == "Needs a call"
    assert (await _row(sf, row.id)).state == "sent"


@pytest.mark.asyncio
async def test_telegram_outreach_fails_for_someone_who_never_started_the_bot(sf):
    row = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(), platform="telegram",
        channel_identifier="mybot", kind="alert", text="hi",
        template_name=None, template_language=None,
    )
    await _send(sf, _Store(), _Delivery())
    saved = await _row(sf, row.id)
    assert saved.state == "failed"
    assert "has not written" in saved.reason


def test_the_opener_note_says_a_team_members_alert_is_about_someone_else():
    member, subject = uuid.uuid4(), uuid.uuid4()
    about_other = SimpleNamespace(template_name="alert", user_id=member, subject_user_id=subject)
    own = SimpleNamespace(template_name="msg", user_id=subject, subject_user_id=subject)
    assert "about another person, not about them" in outreach._opener_note(about_other)
    assert outreach._opener_note(own) == "[Outreach opener sent: template 'msg']"
