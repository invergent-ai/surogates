"""The agent in the loop: it writes each step, reads the step's context on
every turn the step causes, and decides what to do when the deadline passes."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from surogates.db.models import (
    DeliveryOutbox,
    Event,
    FollowupEnrollmentRow,
    OutreachMessageRow,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
    Session as SessionRow,
    User,
)
from surogates.followup import store as followup_store
from surogates.programs import opener as opener_mod
from surogates.programs.delivery import record_outbox_result
from surogates.programs.inbound import open_invitation_for
from surogates.programs.materialize import (
    materialize_occurrence,
    send_queued_openers,
    wake_at_deadlines,
)
from surogates.programs.opener import Woken, make_opener_enqueue, make_step_waker
from surogates.programs.step_context import step_context
from surogates.programs.ticker import sweep_deadlines
from surogates.session.events import SILENT_REPLY, EventType

ORG = uuid.uuid4()
AGENT = "a1"
# Wednesday 2026-10-14 06:00 UTC is 09:00 in Bucharest.
NOW = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
SESSION = uuid.uuid4()
_SLOTS = iter(range(10**6))


def _config(**over):
    return {
        "name": "Daily check", "skill_ref": "daily-check", "channel": "telegram",
        "channel_identifier": "bot1", "timezone": "Europe/Bucharest",
        "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "times_local": ["09:00"], "response_deadline_hours": 24,
        "opener_text": "How are you today?",
        "audience": {"protocol": "recovery", "day_from": -3, "day_to": 30},
        "journey": {"slug": "recovery", "name": "Recovery"},
        **over,
    }


async def _program(sf, **over) -> SimpleNamespace:
    program_id = uuid.uuid4()
    config = _config(**over)
    async with sf() as db:
        db.add(ProgramScheduleRow(
            program_id=program_id, org_id=ORG, agent_id=AGENT, active=True, config=config,
        ))
        await db.commit()
    return SimpleNamespace(program_id=program_id, org_id=ORG, agent_id=AGENT, config=config)


async def _person(sf, *, start=date(2026, 10, 15), protocol="recovery", status="active", **extra):
    user = uuid.uuid4()
    async with sf() as db:
        db.add(User(id=user, org_id=ORG, email=f"{user}@x", display_name="Ion"))
        db.add(FollowupEnrollmentRow(
            org_id=ORG, agent_id=AGENT, user_id=user, protocol=protocol,
            start_date=start, enrolled_by="agent", status=status,
            patient_name="Ion Popescu", **extra,
        ))
        await db.commit()
    return user


async def _invitation(sf, program, user, **fields):
    async with sf() as db:
        occ = ProgramOccurrenceRow(
            program_id=program.program_id, org_id=ORG, agent_id=AGENT,
            scheduled_for=fields.pop("scheduled_for", NOW - timedelta(seconds=next(_SLOTS))),
            skill_ref="daily-check", status="fired",
        )
        db.add(occ)
        await db.flush()
        row = ProgramInvitationRow(
            occurrence_id=occ.id, program_id=program.program_id, org_id=ORG, agent_id=AGENT,
            user_id=user, platform=fields.pop("platform", "telegram"),
            platform_user_id=fields.pop("platform_user_id", "555"),
            channel_identifier="bot1", escalation_state="none",
            **{"delivery_state": "queued", "response_state": "not_started", **fields},
        )
        db.add(row)
        await db.commit()
        return row


async def _reload(sf, inv_id):
    async with sf() as db:
        return await db.get(ProgramInvitationRow, inv_id)


def _granted(pid):
    return SimpleNamespace(
        platform_user_id=pid, platform_meta={"contact_permission": {"status": "granted"}},
    )


async def _lookup(org_id, platform, user_id):
    return _granted("555")


class _Store:
    def __init__(self):
        self.emitted = []

    async def emit_synthetic_user_message(self, session_id, **kw):
        self.emitted.append((session_id, kw))
        return 7


class _Delivery:
    def __init__(self):
        self.calls = []

    async def enqueue(self, *args):
        self.calls.append(args)
        return 99


def _wiring(sf, monkeypatch):
    async def _dm_session(*args, **kwargs):
        return SESSION

    monkeypatch.setattr(opener_mod, "dm_session", _dm_session)
    woke = []

    async def _wake_session(org_id, agent_id, session_id):
        woke.append(session_id)

    async def _config_for(program_id):
        async with sf() as db:
            return (await db.execute(
                sa.select(ProgramScheduleRow.config)
                .where(ProgramScheduleRow.program_id == program_id)
            )).scalar_one_or_none()

    store, delivery = _Store(), _Delivery()
    waker = make_step_waker(
        session_store=store, redis=None, session_factory=sf,
        wake_session=_wake_session, config_for_program=_config_for,
    )
    enqueue = make_opener_enqueue(
        session_store=store, redis=None, session_factory=sf, delivery_service=delivery,
        config_for_program=_config_for, waker=waker,
    )
    return SimpleNamespace(store=store, delivery=delivery, woke=woke, waker=waker, enqueue=enqueue)


async def _wrote(sf, user_pid, *, platform, sender, at):
    await followup_store.record_inbound(
        sf, org_id=ORG, platform=platform, platform_user_id=user_pid,
        channel_identifier=sender, at=at,
    )


# ---------------------------------------------------------------------------
# The agent writes the step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_due_step_wakes_the_agent_instead_of_sending_the_opener(sf, monkeypatch):
    w = _wiring(sf, monkeypatch)
    program = await _program(sf)
    user = await _person(sf)
    await _wrote(sf, "555", platform="telegram", sender="bot1", at=NOW - timedelta(days=2))
    inv = await _invitation(sf, program, user)

    await send_queued_openers(sf, identity_lookup=_lookup, now=NOW, enqueue=w.enqueue)

    row = await _reload(sf, inv.id)
    assert w.delivery.calls == []  # no word-for-word opener
    assert w.woke == [SESSION]
    assert (row.response_state, row.delivery_state, row.outbox_id) == ("awaiting_reply", "queued", None)
    assert row.session_id == SESSION and row.woken_at is not None and row.opened_at is not None
    [(session_id, kw)] = w.store.emitted
    assert kw["synthetic"] == "checkin" and kw["metadata"]["step_event"] == "due"
    assert "«How are you today?»" in kw["content"]
    assert "'daily-check' skill" in kw["content"]


@pytest.mark.asyncio
async def test_whatsapp_outside_its_window_still_opens_with_the_template(sf, monkeypatch):
    w = _wiring(sf, monkeypatch)
    program = await _program(sf, channel="whatsapp", channel_identifier="pn1",
                             template_name="t", template_language="ro")
    user = await _person(sf)
    inv = await _invitation(sf, program, user, platform="whatsapp", platform_user_id="4070")
    inv.channel_identifier = "pn1"

    assert await w.enqueue(inv) == 99
    assert w.woke == [] and w.store.emitted[0][1]["synthetic"] == "checkin_opener"


@pytest.mark.asyncio
async def test_whatsapp_inside_its_window_lets_the_agent_write(sf, monkeypatch):
    w = _wiring(sf, monkeypatch)
    program = await _program(sf, channel="whatsapp", channel_identifier="pn1",
                             template_name="t", template_language="ro")
    user = await _person(sf)
    await _wrote(sf, "4070", platform="whatsapp", sender="pn1",
                 at=datetime.now(timezone.utc) - timedelta(hours=1))
    inv = await _invitation(sf, program, user, platform="whatsapp", platform_user_id="4070")
    inv.channel_identifier = "pn1"

    assert await w.enqueue(inv) == Woken(SESSION)
    assert w.delivery.calls == []


async def _woken_step(sf, monkeypatch):
    """A step the agent was woken to write, and its conversation's events so far."""
    w = _wiring(sf, monkeypatch)
    program = await _program(sf)
    user = await _person(sf)
    await _wrote(sf, "555", platform="telegram", sender="bot1", at=NOW - timedelta(days=1))
    inv = await _invitation(sf, program, user)
    await send_queued_openers(sf, identity_lookup=_lookup, now=NOW, enqueue=w.enqueue)
    async with sf() as db:
        db.add(SessionRow(id=SESSION, org_id=ORG, agent_id=AGENT, channel="telegram", config={}))
        db.add(Event(id=1, session_id=SESSION, type="user.message", data={
            "content": "ctx", "synthetic": "checkin", "invitation_id": str(inv.id),
            "step_event": "due",
        }))
        await db.commit()
    return inv


async def _then(sf, *events):
    """Append *events* (``(id, type, data)``); the last is the agent's
    message, which goes to the outbox."""
    async with sf() as db:
        for eid, kind, data in events:
            db.add(Event(id=eid, session_id=SESSION, type=kind, data=data))
        last = events[-1][0]
        db.add(DeliveryOutbox(
            id=500 + last, session_id=SESSION, event_id=last, channel="telegram",
            destination={}, payload={"content": "Hi"}, dedupe_key=f"telegram:{last}",
            status="pending",
        ))
        await db.commit()
    return 500 + last


@pytest.mark.asyncio
async def test_the_agents_message_is_matched_back_to_its_step(sf, monkeypatch):
    inv = await _woken_step(sf, monkeypatch)
    outbox = await _then(sf, (2, "harness.wake", {}), (3, "llm.response", {}))

    assert await record_outbox_result(sf, outbox, provider_message_id="m1", error=None)
    row = await _reload(sf, inv.id)
    assert (row.outbox_id, row.delivery_state) == (outbox, "delivered")


@pytest.mark.asyncio
async def test_a_wake_taken_with_the_persons_own_message_is_not_the_steps(sf, monkeypatch):
    # The wake waited behind a question; the person's answer came first.
    inv = await _woken_step(sf, monkeypatch)
    outbox = await _then(
        sf, (2, "user.message", {"content": "yes"}), (3, "harness.wake", {}),
        (4, "llm.response", {}),
    )
    assert not await record_outbox_result(sf, outbox, provider_message_id="m1", error=None)
    assert (await _reload(sf, inv.id)).outbox_id is None


@pytest.mark.asyncio
async def test_a_later_turns_message_is_not_the_steps(sf, monkeypatch):
    inv = await _woken_step(sf, monkeypatch)
    outbox = await _then(
        sf, (2, "harness.wake", {}), (3, "llm.response", {}),
        (4, "user.message", {"content": "thanks"}), (5, "harness.wake", {}),
        (6, "llm.response", {}),
    )
    assert not await record_outbox_result(sf, outbox, provider_message_id="m1", error=None)
    assert (await _reload(sf, inv.id)).outbox_id is None


@pytest.mark.asyncio
async def test_a_step_the_agent_never_wrote_says_so_at_the_deadline(sf):
    program = await _program(sf)
    inv = await _invitation(
        sf, program, uuid.uuid4(), response_state="awaiting_reply",
        deadline_at=NOW - timedelta(minutes=1), woken_at=NOW - timedelta(days=1),
    )
    await sweep_deadlines(sf, now=NOW)
    row = await _reload(sf, inv.id)
    assert (row.delivery_state, row.response_state) == ("failed", "not_started")
    assert row.delivery_error == "The agent wrote no message for this step"


def test_the_silence_reply_reaches_no_channel():
    from surogates.session.store import _build_channel_payload

    said = {"message": {"content": f" {SILENT_REPLY} "}}
    assert _build_channel_payload(EventType.LLM_RESPONSE, said, "telegram") == {}
    spoke = {"message": {"content": "Hello"}}
    assert _build_channel_payload(EventType.LLM_RESPONSE, spoke, "telegram")["content"] == "Hello"


# ---------------------------------------------------------------------------
# The step context block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_step_context_names_the_day_before_the_start_and_why(sf):
    program = await _program(sf)
    user = await _person(sf, answered_by="Ana, daughter")
    await _invitation(
        sf, program, user, response_state="completed", outcome="green",
        delivery_state="delivered", scheduled_for=NOW - timedelta(days=1),
    )
    inv = await _invitation(sf, program, user, session_id=SESSION)
    async with sf() as db:
        db.add(SessionRow(id=SESSION, org_id=ORG, agent_id=AGENT, channel="telegram", config={}))
        db.add(Event(session_id=SESSION, type="user.message", data={
            "content": "Pain: 2\nSlept: well", "source": {"flow_reply": True},
        }))
        await db.commit()

    text = await step_context(sf, inv, event="due", now=NOW, config=program.config)

    assert text.startswith("[Step context, from the platform; the person did not write this]")
    assert "Journey: Recovery · Step: Daily check" in text
    assert "Day -1 of the journey (day 0 is 2026-10-15, so this is before it)" in text
    assert "Wednesday 2026-10-14 09:00 (Europe/Bucharest)" in text
    assert (
        "Person: Ion Popescu · Writing: Ion Popescu (the person followed)"
        " · Usually answered by Ana, daughter"
    ) in text
    assert "completed (green)" in text
    assert "Last form answers: Pain: 2 Slept: well" in text
    assert "this step is due now" in text and SILENT_REPLY in text


@pytest.mark.asyncio
async def test_the_step_context_finds_the_form_answers_past_messages_with_another_source(sf):
    program = await _program(sf)
    inv = await _invitation(sf, program, await _person(sf), session_id=SESSION)
    async with sf() as db:
        db.add(SessionRow(id=SESSION, org_id=ORG, agent_id=AGENT, channel="web", config={}))
        db.add(Event(session_id=SESSION, type="user.message", data={
            "content": "Pain: 2\nSlept: well", "source": {"flow_reply": True},
        }))
        db.add(Event(session_id=SESSION, type="user.message", data={
            "content": "[Comment on Plan.docx] Ion: «@Writer is Q1 right?»", "source": "office_mention",
        }))
        db.add(Event(session_id=SESSION, type="user.message", data={
            "content": "Approved", "source": "inbox_governance_decision",
        }))
        await db.commit()

    text = await step_context(sf, inv, event="reply", now=NOW, config=program.config)

    assert "Last form answers: Pain: 2 Slept: well" in text


@pytest.mark.asyncio
async def test_the_step_context_says_which_contact_is_writing(sf):
    program = await _program(sf)
    person = await _person(sf)
    daughter = uuid.uuid4()
    async with sf() as db:
        db.add(User(id=daughter, org_id=ORG, email="ana@x", display_name="Ana"))
        await db.commit()
    await followup_store.link_contact(
        sf, org_id=ORG, agent_id=AGENT, person_user_id=person, contact_user_id=daughter,
        relation="daughter", created_by="agent",
    )
    inv = await _invitation(sf, program, daughter, person_user_id=person)
    text = await step_context(sf, inv, event="reply", now=NOW, config=program.config)
    assert "Person: Ion Popescu · Writing: Ana (daughter), for Ion Popescu" in text
    assert "Usually answered by" not in text


@pytest.mark.asyncio
async def test_the_deadline_context_says_when_the_agent_cannot_write(sf):
    program = await _program(sf)
    inv = await _invitation(sf, program, await _person(sf))
    text = await step_context(
        sf, inv, event="deadline", now=NOW, config=program.config, can_write=False,
    )
    assert "deadline (24h) passed with no reply" in text
    assert "would not reach them now" in text


# ---------------------------------------------------------------------------
# The deadline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_agent_is_woken_once_at_the_deadline(sf, monkeypatch):
    w = _wiring(sf, monkeypatch)
    program = await _program(sf)
    user = await _person(sf)
    inv = await _invitation(
        sf, program, user, response_state="no_reply_by_deadline", delivery_state="delivered",
        deadline_at=NOW - timedelta(minutes=1), session_id=SESSION,
    )
    old = await _invitation(
        sf, program, await _person(sf), response_state="no_reply_by_deadline",
        delivery_state="delivered", deadline_at=NOW - timedelta(days=3),
    )

    assert await wake_at_deadlines(sf, now=NOW, wake=w.waker) == 1
    assert await wake_at_deadlines(sf, now=NOW + timedelta(minutes=1), wake=w.waker) == 0
    assert w.woke == [SESSION]
    [(_, kw)] = w.store.emitted
    assert kw["metadata"]["step_event"] == "deadline"
    assert (await _reload(sf, inv.id)).deadline_woken_at is not None
    assert (await _reload(sf, old.id)).deadline_woken_at is None  # long past: left alone


@pytest.mark.asyncio
async def test_a_deadline_wake_waits_for_quiet_hours_and_skips_an_ended_journey(sf, monkeypatch):
    w = _wiring(sf, monkeypatch)
    program = await _program(sf, journey={
        "slug": "recovery", "quiet_hours": {"start": "08:00", "end": "10:00"},
    })
    quiet = await _invitation(
        sf, program, await _person(sf), response_state="no_reply_by_deadline",
        delivery_state="delivered", deadline_at=NOW - timedelta(minutes=1),
    )
    ended = await _invitation(
        sf, program, await _person(sf, status="ended"), response_state="no_reply_by_deadline",
        delivery_state="delivered", deadline_at=NOW - timedelta(minutes=1),
    )
    assert await wake_at_deadlines(sf, now=NOW, wake=w.waker) == 0
    assert (await _reload(sf, quiet.id)).deadline_woken_at is None
    assert (await _reload(sf, ended.id)).deadline_woken_at is not None  # claimed, never woken
    assert await wake_at_deadlines(sf, now=NOW + timedelta(hours=2), wake=w.waker) == 1


@pytest.mark.asyncio
async def test_the_team_alert_gives_the_agent_time_and_stands_down_when_it_acted(sf):
    from surogates.followup.missed import alert_missed_checkins

    program = await _program(sf, alert_after_missed=1)
    user = await _person(sf)
    team = uuid.uuid4()
    await followup_store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, channel_identifier="pn1",
        alert_recipient_user_ids=[team], alert_template_name="a", alert_template_language="ro",
    )
    from surogates.db.models import ChannelIdentity
    async with sf() as db:
        db.add(ChannelIdentity(
            org_id=ORG, user_id=team, platform="whatsapp", platform_user_id="4079",
            platform_meta={"contact_permission": {"status": "granted"}},
        ))
        await db.commit()
    handled = await _invitation(
        sf, program, user, response_state="no_reply_by_deadline", delivery_state="delivered",
        deadline_at=NOW - timedelta(minutes=30), deadline_woken_at=NOW - timedelta(minutes=5),
    )
    assert await alert_missed_checkins(sf, now=NOW) == 0  # the agent is still deciding
    async with sf() as db:
        (await db.get(ProgramInvitationRow, handled.id)).escalation_state = "handled_by_agent"
        await db.commit()
    assert await alert_missed_checkins(sf, now=NOW + timedelta(minutes=20)) == 0

    other = await _person(sf)
    await _invitation(
        sf, program, other, response_state="no_reply_by_deadline", delivery_state="delivered",
        deadline_at=NOW - timedelta(minutes=30), deadline_woken_at=NOW - timedelta(minutes=5),
    )
    assert await alert_missed_checkins(sf, now=NOW + timedelta(minutes=20)) == 1


async def _alerting(sf):
    """A step that alerts the team after one missed reply, and a team to tell."""
    from surogates.db.models import ChannelIdentity

    program = await _program(sf, alert_after_missed=1)
    team = uuid.uuid4()
    await followup_store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, channel_identifier="pn1",
        alert_recipient_user_ids=[team], alert_template_name="a", alert_template_language="ro",
    )
    async with sf() as db:
        db.add(ChannelIdentity(
            org_id=ORG, user_id=team, platform="whatsapp", platform_user_id="4079",
            platform_meta={"contact_permission": {"status": "granted"}},
        ))
        await db.commit()
    return program


@pytest.mark.asyncio
async def test_the_team_alert_waits_for_a_wake_quiet_hours_put_off(sf):
    from surogates.followup.missed import alert_missed_checkins

    program = await _alerting(sf)
    await _invitation(
        sf, program, await _person(sf), response_state="no_reply_by_deadline",
        delivery_state="delivered", deadline_at=NOW - timedelta(hours=3),
    )
    # Not woken yet (held through the night): the agent goes first.
    assert await alert_missed_checkins(sf, now=NOW) == 0
    # A wake that never comes (no send pass for half a day) gives way.
    assert await alert_missed_checkins(sf, now=NOW + timedelta(hours=10)) == 1


@pytest.mark.asyncio
async def test_a_nudge_after_the_deadline_wake_stands_the_alert_down(sf):
    from surogates.followup.missed import alert_missed_checkins

    program = await _alerting(sf)
    nudged = await _invitation(
        sf, program, await _person(sf), response_state="no_reply_by_deadline",
        delivery_state="delivered", deadline_at=NOW - timedelta(hours=1),
        deadline_woken_at=NOW - timedelta(minutes=30), session_id=SESSION,
    )
    async with sf() as db:
        db.add(SessionRow(id=SESSION, org_id=ORG, agent_id=AGENT, channel="telegram", config={}))
        db.add(Event(id=10, session_id=SESSION, type="user.message", data={
            "content": "ctx", "synthetic": "checkin",
            "invitation_id": str(nudged.id), "step_event": "deadline",
        }))
        db.add(Event(id=11, session_id=SESSION, type="llm.response", data={}))
        db.add(DeliveryOutbox(
            id=900, session_id=SESSION, event_id=11, channel="telegram", destination={},
            payload={"content": "Just checking in"}, dedupe_key="telegram:11", status="pending",
        ))
        await db.commit()
    assert await alert_missed_checkins(sf, now=NOW) == 0
    assert (await _reload(sf, nudged.id)).escalation_state == "handled_by_agent"


@pytest.mark.asyncio
async def test_a_paused_journey_stands_the_alert_down(sf):
    from surogates.followup.missed import alert_missed_checkins

    program = await _alerting(sf)
    paused = await _invitation(
        sf, program, await _person(sf, status="paused"), response_state="no_reply_by_deadline",
        delivery_state="delivered", deadline_at=NOW - timedelta(hours=1),
        deadline_woken_at=NOW - timedelta(minutes=30),
    )
    assert await alert_missed_checkins(sf, now=NOW) == 0
    assert (await _reload(sf, paused.id)).escalation_state == "handled_by_agent"


# ---------------------------------------------------------------------------
# step_outcome
# ---------------------------------------------------------------------------


def _tool_kwargs(sf, session_id=SESSION):
    return {"session_id": session_id, "session_factory": sf}


async def _session(sf, user=None, sid=SESSION):
    async with sf() as db:
        db.add(SessionRow(id=sid, org_id=ORG, agent_id=AGENT, channel="telegram",
                          user_id=user, config={}))
        await db.commit()


@pytest.mark.asyncio
async def test_the_agent_may_skip_a_step_it_was_woken_to_write(sf):
    from surogates.tools.builtin.checkin import handle_checkin_outcome

    program = await _program(sf)
    await _session(sf)
    inv = await _invitation(
        sf, program, uuid.uuid4(), response_state="awaiting_reply",
        session_id=SESSION, woken_at=NOW, deadline_at=NOW + timedelta(hours=24),
    )
    assert "reason" in await handle_checkin_outcome({"outcome": "skipped"}, **_tool_kwargs(sf))
    await handle_checkin_outcome(
        {"outcome": "skipped", "label": "green",
         "note": "They wrote an hour ago; nothing new to ask"},
        **_tool_kwargs(sf),
    )
    row = await _reload(sf, inv.id)
    assert (row.response_state, row.delivery_state, row.deadline_at) == (
        "skipped_by_agent", "skipped", None,
    )
    assert row.outcome is None
    assert row.reason == "They wrote an hour ago; nothing new to ask"


@pytest.mark.asyncio
async def test_after_the_deadline_skipping_records_the_agents_decision(sf):
    from surogates.tools.builtin.checkin import handle_checkin_outcome

    program = await _program(sf)
    await _session(sf)
    inv = await _invitation(
        sf, program, uuid.uuid4(), response_state="no_reply_by_deadline",
        delivery_state="delivered", session_id=SESSION,
        deadline_woken_at=datetime.now(timezone.utc),
    )
    await handle_checkin_outcome({"outcome": "skipped", "note": "Away, back Monday"}, **_tool_kwargs(sf))
    row = await _reload(sf, inv.id)
    assert (row.response_state, row.escalation_state) == ("no_reply_by_deadline", "handled_by_agent")


@pytest.mark.asyncio
async def test_a_step_answered_on_another_channel_is_closed_from_there(sf):
    from surogates.tools.builtin.checkin import handle_checkin_outcome

    program = await _program(sf)
    user = uuid.uuid4()
    await _session(sf, user=user, sid=uuid.uuid4())
    other = uuid.uuid4()
    await _session(sf, user=user, sid=other)
    inv = await _invitation(sf, program, user, response_state="replied", session_id=uuid.uuid4())
    result = await handle_checkin_outcome({"outcome": "completed"}, **_tool_kwargs(sf, other))
    assert result.startswith("Step recorded as completed")
    assert (await _reload(sf, inv.id)).response_state == "completed"


def test_the_old_tool_name_still_reaches_the_tool():
    from surogates.runtime.governance import rename_tool_calls

    calls = [{"function": {"name": "checkin_outcome"}}]
    rename_tool_calls(calls)
    assert calls[0]["function"]["name"] == "step_outcome"


# ---------------------------------------------------------------------------
# One open question per journey, and which step a reply answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_open_step_holds_its_own_journey_not_another(sf):
    user = await _person(sf, protocol="recovery")
    async with sf() as db:
        db.add(FollowupEnrollmentRow(
            org_id=ORG, agent_id=AGENT, user_id=user, protocol="billing",
            start_date=date(2026, 10, 15), enrolled_by="agent",
        ))
        await db.commit()
    await _wrote(sf, "555", platform="telegram", sender="bot1", at=NOW - timedelta(days=1))
    recovery = await _program(sf)
    await _invitation(sf, recovery, user, response_state="awaiting_reply", delivery_state="delivered")
    billing = await _program(sf, audience={"protocol": "billing"}, journey={"slug": "billing"})
    same = await _program(sf)

    async def lookup(org_id, platform, user_id):
        return _granted("555")

    for schedule, state in ((billing, "queued"), (same, "skipped")):
        occ = await materialize_occurrence(
            schedule, session_factory=sf, identity_lookup=lookup, now=NOW,
        )
        async with sf() as db:
            [row] = (await db.execute(
                sa.select(ProgramInvitationRow).where(ProgramInvitationRow.occurrence_id == occ)
            )).scalars().all()
        assert row.delivery_state == state, row.reason


@pytest.mark.asyncio
async def test_a_reply_answers_the_most_recently_opened_step_on_any_channel(sf):
    user = uuid.uuid4()
    first = await _program(sf)
    second = await _program(sf, journey={"slug": "billing"})
    older = await _invitation(
        sf, first, user, response_state="awaiting_reply", opened_at=NOW - timedelta(hours=2),
        platform="whatsapp", platform_user_id="4070",
    )
    newer = await _invitation(
        sf, second, user, response_state="awaiting_reply", opened_at=NOW - timedelta(hours=1),
        platform="whatsapp", platform_user_id="4070",
    )
    # Written on Telegram, about a step that went out on WhatsApp.
    found = await open_invitation_for(
        sf, org_id=ORG, agent_id=AGENT, platform="telegram", platform_user_id="555", user_id=user,
    )
    assert found.id == newer.id
    async with sf() as db:
        (await db.get(ProgramInvitationRow, newer.id)).response_state = "completed"
        await db.commit()
    found = await open_invitation_for(
        sf, org_id=ORG, agent_id=AGENT, platform="telegram", platform_user_id="555", user_id=user,
    )
    assert found.id == older.id


# ---------------------------------------------------------------------------
# Quiet hours hold the team's messages to a person
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_team_message_waits_for_quiet_hours_to_end_and_an_alert_never_does(sf, monkeypatch):
    from surogates.followup import store as store_mod

    await _program(sf, journey={"slug": "recovery", "quiet_hours": {"start": "21:00", "end": "08:00"}})
    user = await _person(sf)
    late = datetime(2026, 10, 14, 20, 30, tzinfo=timezone.utc)  # 23:30 in Bucharest

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return late

    monkeypatch.setattr(store_mod, "datetime", _Clock)
    base = dict(org_id=ORG, agent_id=AGENT, user_id=user, platform="whatsapp",
                channel_identifier="pn1", template_name=None, template_language=None)
    held = await store_mod.queue_outreach(sf, **base, kind="operator", text="Call us")
    now = await store_mod.queue_outreach(sf, **base, kind="operator", text="Now", send_now=True)
    alert = await store_mod.queue_outreach(sf, **base, kind="alert", text="Fever")
    assert held.not_before == datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)  # 08:00 their time, stored as UTC
    assert now.not_before is None and alert.not_before is None

    from surogates.followup.outreach import send_queued_outreach

    seen = []

    async def _never(*a, **k):
        seen.append(a[1].id)
        return 0

    monkeypatch.setattr("surogates.followup.outreach._send_one", _never)
    await send_queued_outreach(
        sf, session_store=None, redis=None, delivery_service=None,
        identity_lookup=_lookup, now=late,
    )
    assert held.id not in seen and {now.id, alert.id} <= set(seen)
    seen.clear()
    await send_queued_outreach(
        sf, session_store=None, redis=None, delivery_service=None,
        identity_lookup=_lookup, now=datetime(2026, 10, 15, 5, 1, tzinfo=timezone.utc),
    )
    assert held.id in seen
    async with sf() as db:
        assert (await db.get(OutreachMessageRow, held.id)).state == "queued"
