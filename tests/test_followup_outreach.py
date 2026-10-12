"""Outreach: text inside the provider window, a template outside it."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from surogates.db.models import OutreachMessageRow
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

    async def emit_event(self, session_id, event_type, data, **_):
        if self.fail_on_emit:
            raise RuntimeError("database went away")
        self.events.append((session_id, event_type, data))
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
            platform="slack", channel_identifier="A1", kind="alert",
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
