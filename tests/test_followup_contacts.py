"""Followed people and their contacts, team permissions and routing."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa

from surogates.db.models import (
    ChannelIdentity,
    EscalationRow,
    FollowupContactRow,
    OutreachMessageRow,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
    Session as SessionRow,
    User,
)
from surogates.followup import people, store
from surogates.programs import funnel, inbound
from surogates.tools.builtin import followup, followup_people, team

ORG = uuid.uuid4()
AGENT = "a1"
GRANTED = {"contact_permission": {"status": "granted"}}


@pytest.fixture(autouse=True)
def _bindings(monkeypatch):
    """agent_users is written with a Postgres upsert; not under test here."""
    async def ensure(db, **kw):
        return None

    monkeypatch.setattr(people, "ensure_agent_user", ensure)


async def _user(sf, name, phone=None, *, permission=True, boundary=None, channel="whatsapp"):
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        db.add(User(id=user_id, org_id=ORG, email=f"{user_id}@x", display_name=name))
        if phone:
            db.add(ChannelIdentity(
                org_id=ORG, user_id=user_id, platform=channel, platform_user_id=phone,
                platform_meta=dict(GRANTED) if permission else {},
            ))
            db.add(SessionRow(
                id=session_id, org_id=ORG, agent_id=AGENT, channel=channel, user_id=user_id,
                config={f"{channel}_channel_id": phone, "channel_identifier": "pn1",
                        **({"memory_boundary": boundary} if boundary else {})},
            ))
        await db.commit()
    return user_id, session_id


@pytest_asyncio.fixture
async def clinic(sf):
    async with sf() as db:
        db.add(ProgramScheduleRow(
            program_id=uuid.uuid4(), org_id=ORG, agent_id=AGENT, active=True,
            config={"audience": {"protocol": "post-op", "day_to": 30},
                    "timezone": "Europe/Bucharest"},
        ))
        await db.commit()
    doctor, doctor_session = await _user(sf, "Dr. Ionescu", "40700000099")
    nurse, _ = await _user(sf, "Asistenta Maria", "40700000098")
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, channel_identifier="pn1",
        alert_recipient_user_ids=[doctor, nurse], alert_template_name="alert",
        alert_template_language="ro", message_template_name="msg",
        message_template_language="ro",
        team_permissions={str(nurse): {"receives": ["urgent", "attention"],
                                       "can": ["see_full_phones"], "handles": "wound care"}},
    )
    return doctor, doctor_session, nurse


def _kw(sf, session_id, **extra):
    return {"session_id": str(session_id), "session_factory": sf, **extra}


async def _all(sf, model):
    async with sf() as db:
        return (await db.execute(sa.select(model))).scalars().all()


async def _enrol_father(sf, ana_session):
    return await followup.handle_followup_enroll(
        {"protocol": "post-op", "days_ago": 3, "consent": True, "summary": "Tatăl Anei.",
         "person_name": "Ion Popescu", "for_someone_else": True, "relation": "fiica"},
        **_kw(sf, ana_session),
    )


# --- the followed person and their contacts ---------------------------------


@pytest.mark.asyncio
async def test_a_relative_enrols_the_person_who_becomes_followed_with_them_as_contact(sf, clinic):
    ana, ana_session = await _user(sf, "Ana", "40711111111")
    result = await _enrol_father(sf, ana_session)
    assert "linked as their fiica" in result

    [row] = await store.enrollments(sf, org_id=ORG, agent_id=AGENT)
    assert row.user_id != ana and row.patient_name == "Ion Popescu"
    recipients = await store.recipients_for(sf, row)
    assert recipients == [
        store.Recipient(row.user_id, "person", None), store.Recipient(ana, "contact", "fiica"),
    ]
    speaker = await store.speaker_for(sf, org_id=ORG, agent_id=AGENT, user_id=ana)
    assert store.writer_line(speaker) == "Writing: Ana (fiica), for Ion Popescu"
    # The team is told who it is about and who reported it.
    [note] = [o for o in await _all(sf, OutreachMessageRow) if o.kind == "enrolment"]
    assert "Ion Popescu" in note.text and "reported by Ana (fiica)" in note.text
    assert note.subject_user_id == row.user_id

    # Enrolling again for the same name reuses the person.
    await _enrol_father(sf, ana_session)
    assert len(await store.enrollments(sf, org_id=ORG, agent_id=AGENT)) == 1


@pytest.mark.asyncio
async def test_a_number_given_by_a_linked_contact_links_on_exact_match_only(sf, clinic):
    ana, ana_session = await _user(sf, "Ana", "40711111111")
    await _enrol_father(sf, ana_session)
    mihai, _ = await _user(sf, "Mihai", "40722222222")
    result = await followup_people.handle_followup_contacts(
        {"action": "add", "phone": "+40 722 222 222", "relation": "fiul"}, **_kw(sf, ana_session),
    )
    assert result.startswith("Linked")
    result = await followup_people.handle_followup_contacts(
        {"action": "add", "phone": "+40 733 333 333", "name": "Dana", "relation": "nora"},
        **_kw(sf, ana_session),
    )
    assert "skipped" in result
    links = await _all(sf, FollowupContactRow)
    assert {c.relation for c in links} == {"fiica", "fiul", "nora"}
    assert mihai in {c.contact_user_id for c in links}
    listed = await followup_people.handle_followup_contacts({"action": "list"}, **_kw(sf, ana_session))
    assert "Mihai (fiul)" in listed and "Dana (nora)" in listed


@pytest.mark.asyncio
async def test_a_newcomers_claim_is_confirmed_by_a_linked_person_in_their_chat(sf, clinic):
    ana, ana_session = await _user(sf, "Ana", "40711111111")
    await _enrol_father(sf, ana_session)
    newcomer, new_session = await _user(sf, "Vlad", "40744444444")
    result = await followup_people.handle_followup_contacts(
        {"action": "claim", "relation": "nepotul", "person_name": "ion popescu"},
        **_kw(sf, new_session),
    )
    assert result.startswith("Asked a person already linked")
    [pending] = [c for c in await _all(sf, FollowupContactRow) if c.status == "pending"]
    assert pending.contact_user_id == newcomer and pending.asked_user_id == ana
    [ask] = [o for o in await _all(sf, OutreachMessageRow) if o.created_by == "contact-claim"]
    assert ask.user_id == ana and "nepotul" in ask.text
    # Pending is not linked: the newcomer speaks for nobody yet.
    assert await store.speaker_for(sf, org_id=ORG, agent_id=AGENT, user_id=newcomer) is None

    result = await followup_people.handle_followup_contacts(
        {"action": "confirm", "answer": True}, **_kw(sf, ana_session),
    )
    assert result.startswith("Linked")
    speaker = await store.speaker_for(sf, org_id=ORG, agent_id=AGENT, user_id=newcomer)
    assert speaker.relation == "nepotul"


@pytest.mark.asyncio
async def test_a_claim_nobody_can_confirm_goes_to_the_team(sf, clinic):
    _vlad, session = await _user(sf, "Vlad", "40744444444")
    result = await followup_people.handle_followup_contacts(
        {"action": "claim", "relation": "fiul", "person_name": "Nimeni Cunoscut"},
        **_kw(sf, session),
    )
    assert "team was asked to confirm" in result
    [escalation] = await _all(sf, EscalationRow)
    assert escalation.kind == "link_request"
    assert not await _all(sf, FollowupContactRow)


async def _occurrence(sf, program_id):
    async with sf() as db:
        occ = ProgramOccurrenceRow(
            program_id=program_id, org_id=ORG, agent_id=AGENT,
            scheduled_for=datetime.now(timezone.utc), skill_ref="s", status="fired",
        )
        db.add(occ)
        await db.commit()
        return occ.id


async def _copy(sf, occ, program_id, user_id, person, *, response="awaiting_reply",
                delivery="delivered", outbox=1):
    async with sf() as db:
        row = ProgramInvitationRow(
            occurrence_id=occ, program_id=program_id, org_id=ORG, agent_id=AGENT,
            user_id=user_id, person_user_id=person, platform="whatsapp",
            platform_user_id=str(user_id)[:8], delivery_state=delivery,
            response_state=response, escalation_state="none", outbox_id=outbox,
        )
        db.add(row)
        await db.commit()
        return row


@pytest.mark.asyncio
async def test_an_answer_from_any_of_them_answers_the_step_for_all(sf, clinic):
    ana, _ = await _user(sf, "Ana", "40711111111")
    person, mihai, dana = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await store.link_contact(sf, org_id=ORG, agent_id=AGENT, person_user_id=person,
                             contact_user_id=ana, relation="fiica", created_by="t")
    program_id = uuid.uuid4()
    occ = await _occurrence(sf, program_id)
    own = await _copy(sf, occ, program_id, person, person)
    answered = await _copy(sf, occ, program_id, ana, person)
    unsent = await _copy(sf, occ, program_id, mihai, person, response="not_started",
                         delivery="queued", outbox=None)
    other_person = await _copy(sf, occ, program_id, dana, dana)

    await inbound.attach_reply(sf, answered, session_id=uuid.uuid4(), bundle_version=None)

    async with sf() as db:
        states = {r.id: r for r in (await db.execute(sa.select(ProgramInvitationRow))).scalars()}
    assert states[answered.id].response_state == "replied"
    assert states[own.id].response_state == "answered_by_other"
    assert states[own.id].reason == "Answered by Ana (fiica)"
    assert (states[unsent.id].response_state, states[unsent.id].delivery_state) == (
        "answered_by_other", "canceled",
    )
    assert states[other_person.id].response_state == "awaiting_reply"

    funnels = await funnel.step_funnel(
        sf, org_id=ORG, agent_id=AGENT, program_ids=[program_id],
        since=datetime.now(timezone.utc) - timedelta(days=1),
    )
    f = funnels[program_id]
    assert (f["invited"], f["replied"], f["no_reply"]) == (2, 1, 0)


@pytest.mark.asyncio
async def test_the_shared_record_is_the_conversation_that_already_holds_it(sf, clinic):
    ana, ana_session = await _user(sf, "Ana", "40711111111", boundary="whatsapp:iso:ana")
    await _enrol_father(sf, ana_session)
    [row] = await store.enrollments(sf, org_id=ORG, agent_id=AGENT)
    for who in (ana, row.user_id):
        assert await store.shared_memory_boundary(
            sf, org_id=ORG, agent_id=AGENT, user_id=who,
        ) == "whatsapp:iso:ana"
    [row] = await store.enrollments(sf, org_id=ORG, agent_id=AGENT)
    assert row.memory_boundary == "whatsapp:iso:ana"
    stranger, _ = await _user(sf, "X", "40755555555")
    assert await store.shared_memory_boundary(sf, org_id=ORG, agent_id=AGENT, user_id=stranger) is None


# --- stopping ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_withdraws_the_channel_ends_the_journey_and_tells_the_team(sf, clinic):
    ion, session = await _user(sf, "Ion", "40711111111")
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=ion, protocol="post-op",
        start_date=date.today(), phase=None, enrolled_by="agent",
    )
    assert people.is_stop(" STOP! ") and not people.is_stop("please stop the pain")
    result = await followup_people.handle_followup_withdraw_consent(
        {"reason": "nu mai vrea mesaje"}, **_kw(sf, session),
    )
    assert "no more messages" in result and "post-op ended" in result
    [identity] = [i for i in await _all(sf, ChannelIdentity) if i.user_id == ion]
    assert store.permission_status(identity) == "withdrawn"
    [row] = await store.enrollments(sf, org_id=ORG, agent_id=AGENT, user_id=ion)
    assert row.status == "ended" and row.status_reason.startswith("Asked to stop on WhatsApp")
    told = [o for o in await _all(sf, OutreachMessageRow) if o.kind == "alert"]
    assert told and "asked to stop" in told[0].text
    # Asking twice changes nothing and tells nobody again.
    again = await followup_people.handle_followup_withdraw_consent({"reason": "x"}, **_kw(sf, session))
    assert "notified" not in again
    assert len([o for o in await _all(sf, OutreachMessageRow) if o.kind == "alert"]) == len(told)


# --- team permissions and routing --------------------------------------------


@pytest.mark.asyncio
async def test_each_member_receives_only_their_kinds_and_the_agent_can_route(sf, clinic):
    doctor, _, nurse = clinic
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    queued, _ = await store.notify_team(
        sf, settings, org_id=ORG, agent_id=AGENT, kind="enrolment", text="new",
        created_by="t", topic="new_enrolments",
    )
    assert queued == 1
    queued, _ = await store.notify_team(
        sf, settings, org_id=ORG, agent_id=AGENT, kind="alert", text="rana",
        created_by="t", topic="attention", to=[nurse],
    )
    assert queued == 1
    rows = await _all(sf, OutreachMessageRow)
    assert {(r.user_id, r.text) for r in rows} == {(doctor, "new"), (nurse, "rana")}
    with pytest.raises(ValueError):
        await store.notify_team(sf, settings, org_id=ORG, agent_id=AGENT, kind="alert",
                                text="x", created_by="t", topic="gossip")


@pytest.mark.asyncio
async def test_a_shared_destination_also_receives_its_kinds(sf, clinic):
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, senders={"slack": "T1"},
        team_destinations=[{"platform": "slack", "channel_id": "C123"}],
    )
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    await store.notify_team(sf, settings, org_id=ORG, agent_id=AGENT, kind="alert",
                            text="urgent", created_by="t", topic="urgent", dedupe_prefix="e1")
    await store.notify_team(sf, settings, org_id=ORG, agent_id=AGENT, kind="enrolment",
                            text="new", created_by="t", topic="new_enrolments")
    shared = [r for r in await _all(sf, OutreachMessageRow) if r.destination]
    assert [(r.destination, r.platform, r.user_id, r.text) for r in shared] == [
        ("C123", "slack", store.NO_PERSON, "urgent"),
    ]
    with pytest.raises(ValueError):
        await store.update_settings(sf, org_id=ORG, agent_id=AGENT,
                                    team_destinations=[{"platform": "whatsapp", "channel_id": "x"}])


@pytest.mark.asyncio
async def test_permissions_default_to_everything_and_refusals_name_the_action(sf, clinic):
    doctor, _, nurse = clinic
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    assert store.member_permissions(settings, doctor)["can"] == frozenset(store.ABILITIES)
    assert store.can(settings, nurse, "see_full_phones")
    assert not store.can(settings, nurse, "see_people")
    assert not store.can(settings, uuid.uuid4(), "see_people")
    nurse_session = uuid.uuid4()
    async with sf() as db:
        db.add(SessionRow(id=nurse_session, org_id=ORG, agent_id=AGENT, channel="whatsapp",
                          user_id=nurse, config={"whatsapp_channel_id": "40700000098",
                                                 "channel_identifier": "pn1"}))
        await db.commit()
    result = await team.handle_team_overview({}, **_kw(sf, nurse_session))
    assert result.startswith("Refused") and "can't see the people followed" in result
    assert "manages the team" in result
    for word in ("Alerts only", "Assistant", "Full", "preset", "role"):
        assert word not in result
    with pytest.raises(ValueError):
        await store.update_settings(sf, org_id=ORG, agent_id=AGENT,
                                    team_permissions={str(nurse): {"can": ["fly"]}})


@pytest.mark.asyncio
async def test_an_older_preset_reads_as_its_lists_and_is_saved_as_lists(sf, clinic):
    doctor, _, nurse = clinic
    async with sf() as db:
        from surogates.db.models import FollowupSettingsRow

        row = await db.get(FollowupSettingsRow, (ORG, AGENT))
        row.team_permissions = {str(doctor): {"preset": "assistant"},
                                str(nurse): {"preset": "alerts_only"}}
        await db.commit()
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    assert not store.can(settings, doctor, "manage_team") and store.can(settings, doctor, "enroll")
    assert store.member_permissions(settings, nurse)["receives"] == {"urgent", "attention"}
    saved = await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, team_permissions=settings.team_permissions,
    )
    assert saved.team_permissions[str(doctor)] == {
        "receives": "all", "can": [a for a in store.ABILITIES if a != "manage_team"],
    }
    assert saved.team_permissions[str(nurse)] == {
        "receives": ["urgent", "attention"], "can": ["see_full_phones"],
    }
    full = await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, team_permissions={str(doctor): {"preset": "full"}},
    )
    assert full.team_permissions[str(doctor)] == {"receives": "all", "can": "all"}


@pytest.mark.asyncio
async def test_escalate_names_its_recipients_by_what_they_handle(sf, clinic):
    doctor, _, nurse = clinic
    ion, session = await _user(sf, "Ion", "40711111111")
    status = await followup.handle_followup_status({}, **_kw(sf, session))
    assert "Asistenta Maria — handles: wound care" in status
    await followup.handle_escalate(
        {"reason": "rana roșie", "to": ["Asistenta Maria"]}, **_kw(sf, session),
    )
    [alert] = [o for o in await _all(sf, OutreachMessageRow) if o.kind == "alert"]
    assert alert.user_id == nurse


@pytest.mark.asyncio
async def test_an_escalation_with_no_known_person_is_still_queued(sf, clinic):
    session_id = uuid.uuid4()
    async with sf() as db:
        db.add(SessionRow(id=session_id, org_id=ORG, agent_id=AGENT, channel="web",
                          user_id=None, config={}))
        await db.commit()
    await followup.handle_escalate({"reason": "cineva are nevoie de ajutor"}, **_kw(sf, session_id))
    [escalation] = await _all(sf, EscalationRow)
    assert escalation.user_id is None and escalation.session_id == session_id


# --- the team enrols on any channel ------------------------------------------


class _Redis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def getdel(self, key):
        return self.data.pop(key, None)


async def _confirmed(handler, arguments, kw):
    preview = await handler(arguments, **kw)
    code = preview.split('confirmation="')[1].split('"')[0]
    return await handler({**arguments, "confirmation": code}, **kw)


@pytest.mark.asyncio
async def test_the_team_enrols_a_slack_member(sf, clinic):
    _doctor, doctor_session, _ = clinic
    await store.update_settings(sf, org_id=ORG, agent_id=AGENT, senders={"slack": "T1"})
    result = await _confirmed(
        team.handle_team_enroll_person,
        {"slack_member": "U01ABCDEF", "person_name": "Radu", "days_ago": 1},
        _kw(sf, doctor_session, redis=_Redis()),
    )
    assert result.startswith("Enrolled Radu (Slack member …CDEF)")
    [identity] = [i for i in await _all(sf, ChannelIdentity) if i.platform == "slack"]
    assert identity.platform_user_id == "U01ABCDEF"
    assert store.permission_status(identity) == "granted"
    [welcome] = [o for o in await _all(sf, OutreachMessageRow) if o.kind == "welcome"]
    assert (welcome.platform, welcome.template_name) == ("slack", None)


@pytest.mark.asyncio
async def test_the_team_enrols_with_a_telegram_start_link(sf, clinic, monkeypatch):
    _doctor, doctor_session, _ = clinic
    monkeypatch.setenv("SUROGATES_TELEGRAM_USERNAME", "clinic_bot")
    redis = _Redis()
    result = await _confirmed(
        team.handle_team_enroll_person,
        {"telegram_link": True, "person_name": "Radu", "date_unknown": True},
        _kw(sf, doctor_session, redis=redis),
    )
    assert "https://t.me/clinic_bot?start=" in result
    [key] = [k for k in redis.data if k.startswith("surogates:person_link:")]
    [row] = await store.enrollments(sf, org_id=ORG, agent_id=AGENT)
    assert str(row.user_id) in redis.data[key]


@pytest.mark.asyncio
async def test_an_assistant_cannot_enrol_without_permission(sf, clinic):
    doctor, doctor_session, _ = clinic
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT,
        team_permissions={str(doctor): {"can": ["see_people"]}},
    )
    result = await team.handle_team_enroll_person(
        {"phone": "+40 711 222 333", "person_name": "X", "days_ago": 1},
        **_kw(sf, doctor_session, redis=_Redis()),
    )
    assert result.startswith("Refused") and "can't enrol people" in result


@pytest.mark.asyncio
async def test_a_missed_streak_is_the_persons_whoever_the_copies_went_to(sf, clinic):
    from surogates.followup.missed import alert_missed_checkins

    ana, _ = await _user(sf, "Ana", "40711111111")
    person = uuid.uuid4()
    program_id = uuid.uuid4()
    async with sf() as db:
        db.add(ProgramScheduleRow(
            program_id=program_id, org_id=ORG, agent_id=AGENT, active=True,
            config={"name": "Zilnic", "channel_identifier": "pn1", "alert_after_missed": 2},
        ))
        await db.commit()
    now = datetime.now(timezone.utc)
    # Never woken at its deadline: the platform alerts 12 hours after it.
    for hours in (40, 14):
        occ = await _occurrence(sf, program_id)
        for who in (person, ana):
            row = await _copy(sf, occ, program_id, who, person, response="no_reply_by_deadline")
            async with sf() as db:
                saved = await db.get(ProgramInvitationRow, row.id)
                saved.created_at = now - timedelta(hours=hours + 1)
                saved.deadline_at = now - timedelta(hours=hours)
                await db.commit()
    assert await alert_missed_checkins(sf, now=now) >= 1
    [escalation] = await _all(sf, EscalationRow)
    assert escalation.user_id == person
    marked = {r.escalation_state for r in await _all(sf, ProgramInvitationRow) if r.created_at > now - timedelta(hours=16)}
    assert marked == {"raised"}
    assert await alert_missed_checkins(sf, now=now) == 0


# --- escalations from chat -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_member_takes_and_resolves_an_escalation_and_the_others_are_told(sf, clinic):
    doctor, doctor_session, nurse = clinic
    ion, session = await _user(sf, "Ion Popescu", "40711111111")
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=ion, protocol="post-op",
        start_date=date.today(), phase=None, enrolled_by="agent", patient_name="Ion Popescu",
    )
    await followup.handle_escalate({"reason": "durere în piept", "severity": "red"},
                                   **_kw(sf, session))
    await store.update_settings(sf, org_id=ORG, agent_id=AGENT,
                                texts={"escalation_taken": "{member} preia cazul lui {name}"})

    # Without a name: the one in the latest alert the member received.
    result = await team.handle_team_escalation({"action": "claim"}, **_kw(sf, doctor_session))
    assert "taken by you" in result and "1 colleague" in result
    [escalation] = await _all(sf, EscalationRow)
    assert escalation.owner == "Dr. Ionescu" and escalation.resolved_at is None
    told = [o for o in await _all(sf, OutreachMessageRow) if o.kind == "notice"]
    assert [(o.user_id, o.text) for o in told] == [(nurse, "Dr. Ionescu preia cazul lui Ion Popescu")]

    result = await team.handle_team_escalation(
        {"action": "resolve", "person": "Popescu"}, **_kw(sf, doctor_session),
    )
    assert "resolved" in result
    [escalation] = await _all(sf, EscalationRow)
    assert escalation.resolved_at is not None
    again = await team.handle_team_escalation(
        {"action": "resolve", "person": "Popescu"}, **_kw(sf, doctor_session),
    )
    assert "no open escalation" in again


@pytest.mark.asyncio
async def test_handling_escalations_is_a_permission(sf, clinic):
    _doctor, _, nurse = clinic
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    assert "handle_escalations" in store.ABILITIES
    assert not store.can(settings, nurse, "handle_escalations")
    nurse_session = uuid.uuid4()
    async with sf() as db:
        db.add(SessionRow(id=nurse_session, org_id=ORG, agent_id=AGENT, channel="whatsapp",
                          user_id=nurse, config={"whatsapp_channel_id": "40700000098",
                                                 "channel_identifier": "pn1"}))
        await db.commit()
    result = await team.handle_team_escalation({"action": "claim"}, **_kw(sf, nurse_session))
    assert result.startswith("Refused") and "take or resolve escalations" in result


@pytest.mark.asyncio
async def test_resolving_from_chat_closes_the_inbox_item_too(sf, clinic):
    from surogates.db.models import InboxItem

    _doctor, doctor_session, _ = clinic
    inbox_sa = uuid.uuid4()
    await store.update_settings(sf, org_id=ORG, agent_id=AGENT, escalation_service_account_id=inbox_sa)
    _ion, session = await _user(sf, "Ion Popescu", "40711111111")
    await followup.handle_escalate({"reason": "febră"}, **_kw(sf, session))
    [item] = await _all(sf, InboxItem)
    assert item.status == "pending"

    result = await team.handle_team_escalation(
        {"action": "resolve"}, **_kw(sf, doctor_session),
    )
    assert "resolved" in result, result
    [item] = await _all(sf, InboxItem)
    assert item.status == "acknowledged" and item.responded_at is not None


# --- the platform's own sentences, in the business's words -----------------------


ROMANIAN = {
    "label_reported_by": "semnalat de {contact} ({relation})",
    "notice_paused": "{journey} pus pe pauză: {reason}",
    "notice_stopped": "{who} a cerut oprirea mesajelor pe {channel}: {what}. {reason}",
    "stop_ended": "{journeys} încheiat",
    "notice_enrolled_by": "{name} înscris în {journey} de {member}.",
}


@pytest.mark.asyncio
async def test_team_notices_use_the_business_wording(sf, clinic):
    _doctor, doctor_session, _ = clinic
    # Everyone receives everything, so the nurse hears of the enrolment too.
    await store.update_settings(sf, org_id=ORG, agent_id=AGENT, texts=ROMANIAN, team_permissions={})
    ana, ana_session = await _user(sf, "Ana", "40711111111")
    await _enrol_father(sf, ana_session)
    notes = [o for o in await _all(sf, OutreachMessageRow) if o.kind == "enrolment"]
    assert notes and all("semnalat de Ana (fiica)" in o.text and "reported" not in o.text for o in notes)

    await followup.handle_followup_update(
        {"protocol": "post-op", "status": "paused", "reason": "internat"}, **_kw(sf, ana_session),
    )
    assert any(o.text.startswith("post-op pus pe pauză: internat") for o in await _all(sf, OutreachMessageRow))

    ion, ion_session = await _user(sf, "Ion", "40722222222")
    await store.upsert_enrollment(sf, org_id=ORG, agent_id=AGENT, user_id=ion, protocol="post-op",
                                  start_date=date.today(), phase=None, enrolled_by="agent")
    await followup_people.handle_followup_withdraw_consent({"reason": "STOP"}, **_kw(sf, ion_session))
    assert any(o.text == "Ion a cerut oprirea mesajelor pe WhatsApp: post-op încheiat. STOP"
               for o in await _all(sf, OutreachMessageRow))

    result = await _confirmed(
        team.handle_team_enroll_person,
        {"phone": "+40 733 333 333", "person_name": "Elena", "days_ago": 1},
        _kw(sf, doctor_session, redis=_Redis()),
    )
    assert result.startswith("Enrolled")
    assert any(o.text.startswith("Elena înscris în post-op de Dr. Ionescu.")
               for o in await _all(sf, OutreachMessageRow))


def test_every_platform_sentence_has_a_key():
    for key, default in store.TEAM_TEXTS.items():
        assert store.wording(None, key, **{k: "x" for k in (
            "answered_by", "contact", "relation", "name", "journey", "reason", "member", "who",
            "channel", "what", "journeys", "writer_name", "writer")}) != default or "{" not in default


@pytest.mark.asyncio
async def test_a_member_whose_channel_has_no_sender_is_told_elsewhere_or_recorded(sf, clinic):
    doctor, _, nurse = clinic
    # The doctor asked for Slack, which has no sender here: WhatsApp instead.
    await store.update_settings(sf, org_id=ORG, agent_id=AGENT,
                                team_channels={str(doctor): ["slack"]}, team_permissions={})
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    queued, why = await store.notify_team(sf, settings, org_id=ORG, agent_id=AGENT,
                                          kind="alert", text="x", created_by="t")
    assert (queued, why) == (2, None)
    assert {r.platform for r in await _all(sf, OutreachMessageRow)} == {"whatsapp"}

    # No sender at all for anyone: each member is recorded as not told, and
    # the reply says so instead of counting them.
    async with sf() as db:
        from surogates.db.models import FollowupSettingsRow

        row = await db.get(FollowupSettingsRow, (ORG, AGENT))
        row.channel_identifier, row.senders = None, {}
        await db.commit()
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    queued, why = await store.notify_team(sf, settings, org_id=ORG, agent_id=AGENT,
                                          kind="alert", text="y", created_by="t")
    assert queued == 0 and "2 team member(s) could not be reached" in why
    failed = [r for r in await _all(sf, OutreachMessageRow) if r.text == "y"]
    assert {r.user_id for r in failed} == {doctor, nurse}
    assert {r.state for r in failed} == {"failed"}
    from surogates.followup.outreach import DEFAULT_TEXTS, plain_reason

    assert plain_reason(failed[0].reason) == DEFAULT_TEXTS["why_no_channel"]


def test_the_reply_counts_honestly():
    from surogates.followup.context import told

    assert told(1, "1 team member(s) could not be reached: x") == (
        "The team was notified (1); 1 team member(s) could not be reached: x."
    )
    assert told(2, None) == "The team was notified (2)."


# --- held messages and the person's channel --------------------------------------


@pytest.mark.asyncio
async def test_a_new_message_from_the_same_member_replaces_their_held_one(sf, clinic, monkeypatch):
    from surogates.programs import journey

    doctor, _, _ = clinic
    ion, _ = await _user(sf, "Ion", "40711111111")
    morning = datetime.now(timezone.utc) + timedelta(hours=9)

    async def quiet(*_a, **_k):
        return (morning, "Australia/Sydney")

    monkeypatch.setattr(journey, "quiet_until", quiet)
    first = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=ion, platform="whatsapp",
        channel_identifier="pn1", kind="operator", text="Veniți mâine la 10.",
        template_name=None, template_language=None, subject_user_id=ion,
        created_by=f"team:{doctor}",
    )
    assert first.not_before is not None
    second = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=ion, platform="whatsapp",
        channel_identifier="pn1", kind="operator", text="Veniți joi, 15 octombrie, la 10.",
        template_name=None, template_language=None, subject_user_id=ion,
        created_by=f"team:{doctor}", send_now=True,
    )
    rows = {r.id: r for r in await _all(sf, OutreachMessageRow)}
    assert rows[first.id].state == "replaced"
    assert (rows[second.id].state, rows[second.id].not_before) == ("queued", None)


@pytest.mark.asyncio
async def test_a_held_message_follows_the_persons_zone_as_it_is_at_send_time(sf, clinic, monkeypatch):
    from surogates.followup import outreach
    from surogates.programs import journey

    doctor, _, _ = clinic
    ion, _ = await _user(sf, "Ion", "40711111111")
    now = datetime.now(timezone.utc)
    held = [(now + timedelta(hours=9), "Australia/Sydney")]

    async def quiet(*_a, **_k):
        return held[0]

    monkeypatch.setattr(journey, "quiet_until", quiet)
    row = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=ion, platform="whatsapp",
        channel_identifier="pn1", kind="operator", text="x",
        template_name=None, template_language=None, subject_user_id=ion,
        created_by=f"team:{doctor}",
    )
    # The person moved: their quiet hours end at another time, then not at all.
    held[0] = (now + timedelta(hours=2), "Europe/Bucharest")
    assert await outreach._refresh_holds(sf, now=now) == 1
    [row] = [r for r in await _all(sf, OutreachMessageRow) if r.id == row.id]
    assert outreach.as_utc(row.not_before) == held[0][0]
    held[0] = None
    assert await outreach._refresh_holds(sf, now=now) == 1
    [row] = [r for r in await _all(sf, OutreachMessageRow) if r.id == row.id]
    assert row.not_before is None
    assert await outreach._refresh_holds(sf, now=now) == 0


def test_the_preferred_channel_is_the_most_recently_set():
    from types import SimpleNamespace as N

    now = datetime.now(timezone.utc)
    rows = [N(preferred_channel="whatsapp", updated_at=now - timedelta(days=1)),
            N(preferred_channel="slack", updated_at=now), N(preferred_channel=None, updated_at=now)]
    assert store.preferred_channel(rows) == "slack"
    assert store.preferred_channel([]) is None


@pytest.mark.asyncio
async def test_a_step_outside_a_journey_uses_the_persons_latest_preference(sf, clinic):
    from types import SimpleNamespace as N

    from surogates.programs.journey import choose_channel

    ion, _ = await _user(sf, "Ion", "40711111111")
    for protocol in ("a", "b"):
        await store.upsert_enrollment(sf, org_id=ORG, agent_id=AGENT, user_id=ion, protocol=protocol,
                                      start_date=None, phase=None, enrolled_by="agent")
    for protocol, channel in (("b", "telegram"), ("a", "whatsapp"), ("b", "telegram")):
        await store.update_enrollment(sf, org_id=ORG, agent_id=AGENT, user_id=ion,
                                      protocol=protocol, preferred_channel=channel)

    async def lookup(org_id, platform, user_id):
        return N(platform_user_id="1", platform_meta=GRANTED)

    config = {"channel": "whatsapp", "channel_identifier": "pn1",
              "journey": {"senders": {"whatsapp": "pn1", "telegram": "bot1"}}}
    from surogates.followup.store import record_inbound

    await record_inbound(sf, org_id=ORG, platform="telegram", platform_user_id="1", channel_identifier="bot1")
    chosen = await choose_channel(config, org_id=ORG, user_id=ion, enrollment=None,
                                  identity_lookup=lookup, session_factory=sf, agent_id=AGENT)
    assert chosen[0] == "telegram"


# --- who the business covers, and people with one name ---------------------------


@pytest.mark.asyncio
async def test_anyone_ever_followed_and_their_contacts_are_covered(sf, clinic):
    doctor, _, _ = clinic
    andrei, _ = await _user(sf, "Andrei", "40711111111")
    ana, _ = await _user(sf, "Ana", "40722222222")
    stranger, _ = await _user(sf, "X", "40733333333")
    await store.upsert_enrollment(sf, org_id=ORG, agent_id=AGENT, user_id=andrei, protocol="post-op",
                                  start_date=None, phase=None, enrolled_by="agent")
    await store.update_enrollment(sf, org_id=ORG, agent_id=AGENT, user_id=andrei, protocol="post-op",
                                  status="ended", status_reason="Asked to stop")
    await store.link_contact(sf, org_id=ORG, agent_id=AGENT, person_user_id=andrei,
                             contact_user_id=ana, relation="fiica", created_by="t")
    covered = lambda uid: store.covered_by_business(sf, org_id=ORG, agent_id=AGENT, user_id=uid)  # noqa: E731
    assert await covered(andrei) and await covered(ana) and await covered(doctor)
    assert not await covered(stranger)


@pytest.mark.asyncio
async def test_two_people_with_one_name_stay_two_in_the_overview(sf, clinic):
    _doctor, doctor_session, _ = clinic
    first, s1 = await _user(sf, "Maria Popescu", "40711111111")
    second, s2 = await _user(sf, "Maria Popescu", "40722222222")
    for uid in (first, second):
        await store.upsert_enrollment(sf, org_id=ORG, agent_id=AGENT, user_id=uid, protocol="post-op",
                                      start_date=date.today(), phase=None, enrolled_by="agent",
                                      patient_name="Maria Popescu")
    for session in (s1, s2):
        await followup.handle_escalate({"reason": "leșin", "severity": "red"}, **_kw(sf, session))
    result = await team.handle_team_overview({}, **_kw(sf, doctor_session))
    assert "Maria Popescu (+40…111)" in result and "Maria Popescu (+40…222)" in result
    assert result.count("alert ") == 2


@pytest.mark.asyncio
async def test_a_team_members_stop_is_said_to_the_agent_not_an_opt_out(sf, clinic):
    doctor, _, _ = clinic
    assert not await people.honour_stop(
        sf, org_id=ORG, agent_id=AGENT, user_id=doctor, platform="whatsapp",
        platform_user_id="40700000099", text="Stop.",
    )
    [identity] = [i for i in await _all(sf, ChannelIdentity) if i.user_id == doctor]
    assert store.permission_status(identity) == "granted"

    ion, _ = await _user(sf, "Ion", "40711111111")
    assert not await people.honour_stop(
        sf, org_id=ORG, agent_id=AGENT, user_id=ion, platform="whatsapp",
        platform_user_id="40711111111", text="please stop the pain",
    )
    assert await people.honour_stop(
        sf, org_id=ORG, agent_id=AGENT, user_id=ion, platform="whatsapp",
        platform_user_id="40711111111", text="STOP",
    )
    [identity] = [i for i in await _all(sf, ChannelIdentity) if i.user_id == ion]
    assert store.permission_status(identity) == "withdrawn"
