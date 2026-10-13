"""team_overview / team_message_person / team_update_person / team_enroll_person."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa

from surogates.db.models import (
    ChannelIdentity,
    Event,
    OutreachMessageRow,
    ProgramInvitationRow,
    ProgramScheduleRow,
    Session as SessionRow,
    User,
)
from surogates.followup import store
from surogates.runtime.memory_protocol import _MemoryEnvelope, encode_envelope
from surogates.tools.builtin import team

ORG = uuid.uuid4()
AGENT = "a1"


async def _person(sf, name, phone, *, org=ORG, agent=AGENT, enrol=True, permission=True):
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    meta = {"contact_permission": {"status": "granted"}} if permission else {}
    async with sf() as db:
        db.add(User(id=user_id, org_id=org, email=f"{user_id}@x", display_name=name))
        db.add(ChannelIdentity(
            org_id=org, user_id=user_id, platform="whatsapp",
            platform_user_id=phone, platform_meta=meta,
        ))
        db.add(SessionRow(
            id=session_id, org_id=org, agent_id=agent, channel="whatsapp",
            user_id=user_id,
            config={"whatsapp_channel_id": phone, "channel_identifier": "pn1"},
        ))
        await db.commit()
    if enrol:
        await store.upsert_enrollment(
            sf, org_id=org, agent_id=agent, user_id=user_id, protocol="post-op",
            start_date=date.today() - timedelta(days=5), phase=None,
            enrolled_by="agent", patient_name=name,
        )
    return user_id, session_id


@pytest_asyncio.fixture
async def practice(sf):
    async with sf() as db:
        db.add(ProgramScheduleRow(
            program_id=uuid.uuid4(), org_id=ORG, agent_id=AGENT, active=True,
            config={"audience": {"protocol": "post-op", "day_to": 30},
                    "timezone": "Europe/Bucharest", "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "times_local": ["08:30"]},
        ))
        await db.commit()
    doctor, doctor_session = await _person(sf, "Dr. Ionescu", "40700000099", enrol=False)
    ion = await _person(sf, "Ion Popescu", "40700000001")
    ioana = await _person(sf, "Ioana Popa", "40700000002")
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, channel_identifier="pn1",
        alert_recipient_user_ids=[doctor], message_template_name="msg",
        message_template_language="ro",
    )
    return SimpleNamespace(
        doctor=doctor, doctor_session=doctor_session, ion=ion, ioana=ioana,
    )


class _Redis:
    def __init__(self):
        self.data, self.ttl = {}, {}

    async def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex

    async def getdel(self, key):
        return self.data.pop(key, None)


_redis = _Redis()


@pytest.fixture(autouse=True)
def _fresh_redis():
    global _redis
    _redis = _Redis()


def _kw(sf, session_id):
    return {"session_id": str(session_id), "session_factory": sf, "redis": _redis}


def _code(preview):
    return re.search(r'confirmation="([0-9A-F]{6})"', preview).group(1)


async def _confirmed(handler, arguments, kw):
    """What the doctor's yes does: the preview, then the same call with its code."""
    preview = await handler(arguments, **kw)
    return await handler({**arguments, "confirmation": _code(preview)}, **kw)


async def _outreach(sf):
    async with sf() as db:
        return (await db.execute(sa.select(OutreachMessageRow))).scalars().all()


# --- gating -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_patient_session_is_refused_by_both_tools(sf, practice):
    _, ion_session = practice.ion
    overview = await team.handle_team_overview({}, **_kw(sf, ion_session))
    sent = await team.handle_team_message_person(
        {"patient": "Ioana", "text": "hi", "confirmed": True}, **_kw(sf, ion_session),
    )
    assert overview == sent == team.NOT_TEAM
    assert "Ioana" not in overview
    assert await _outreach(sf) == []


@pytest.mark.asyncio
async def test_tools_are_offered_only_to_the_care_team_with_programs_on(sf, practice):
    async with sf() as db:
        doctor = await db.get(SessionRow, practice.doctor_session)
        patient = await db.get(SessionRow, practice.ion[1])

    async def excluded(session, programs):
        return await team.team_tool_exclusions(
            sf, session, programs_enabled=programs,
        )

    assert await excluded(doctor, True) == frozenset()
    assert await excluded(doctor, False) == team.TEAM_TOOL_NAMES
    assert await excluded(patient, True) == team.TEAM_TOOL_NAMES
    assert await excluded(SimpleNamespace(user_id=None), True) == team.TEAM_TOOL_NAMES


# --- overview -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_overview_lists_this_agents_people_and_nobody_elses(sf, practice):
    await _person(sf, "Other Agent Patient", "40700000003", agent="a2")
    await _person(sf, "Other Org Patient", "40700000004", org=uuid.uuid4())
    ion, ion_session = practice.ion
    now = datetime.now(timezone.utc)
    async with sf() as db:
        db.add(ProgramInvitationRow(
            occurrence_id=uuid.uuid4(), program_id=uuid.uuid4(), org_id=ORG,
            agent_id=AGENT, user_id=ion, platform="whatsapp",
            platform_user_id="40700000001", delivery_state="delivered",
            response_state="no_reply_by_deadline", created_at=now - timedelta(days=1),
        ))
        db.add(ProgramInvitationRow(
            occurrence_id=uuid.uuid4(), program_id=uuid.uuid4(), org_id=ORG,
            agent_id=AGENT, user_id=ion, platform="whatsapp",
            platform_user_id="40700000001", delivery_state="delivered",
            response_state="completed", reason="TA 150/90",
            created_at=now - timedelta(days=2),
        ))
        db.add(Event(
            session_id=ion_session, org_id=ORG, type="followup.escalation",
            data={"reason": "Durere în piept"},
        ))
        await db.commit()
    for recipient in (practice.doctor, uuid.uuid4()):
        await store.queue_outreach(
            sf, org_id=ORG, agent_id=AGENT, user_id=recipient, platform="whatsapp",
            channel_identifier="pn1", kind="alert",
            text="Durere în piept\n\n— Ion Popescu · +40 700 000 001",
            template_name=None, template_language=None, subject_user_id=ion,
        )
    await store.record_inbound(
        sf, org_id=ORG, platform="whatsapp", platform_user_id="40700000002",
        channel_identifier="pn1", at=now - timedelta(days=4),
    )

    out = await team.handle_team_overview(
        {}, **_kw(sf, practice.doctor_session),
    )
    assert "Ion Popescu" in out and "Ioana Popa" in out
    assert "Other Agent" not in out and "Other Org" not in out
    assert "Dr. Ionescu" not in out  # the care team is not a patient
    assert "post-op: day 5" in out
    assert "unanswered check-ins in a row: 1" in out
    assert out.count("Durere în piept") == 1
    assert "New enrolments (last 24h): " in out and "Ion Popescu (post-op)" in out
    assert "Silent 3 days or more: Ioana Popa (wrote 4d ago)" in out
    assert "next message none" not in out
    assert "08:30 Europe/Bucharest" in out
    assert "latest conversation" not in out


@pytest.mark.asyncio
async def test_overview_focus_shows_one_person_with_their_conversation(sf, practice):
    _, ion_session = practice.ion
    async with sf() as db:
        for kind, data in (
            ("user.message", {"content": "Mă doare capul"}),
            ("user.message", {"content": "[Check-in] Run the skill", "synthetic": "checkin"}),
            ("llm.response", {"message": {"role": "assistant", "content": "Am notat."}}),
        ):
            db.add(Event(session_id=ion_session, org_id=ORG, type=kind, data=data))
        await db.commit()

    out = await team.handle_team_overview(
        {"patient": "popescu"}, **_kw(sf, practice.doctor_session),
    )
    assert "Ioana" not in out
    assert "them: Mă doare capul" in out
    assert "agent: Am notat." in out
    assert "[Check-in]" not in out


# --- message ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ambiguous_patient_returns_candidates_and_sends_nothing(sf, practice):
    out = await team.handle_team_message_person(
        {"patient": "pop", "text": "Veniți mâine.", "confirmed": True},
        **_kw(sf, practice.doctor_session),
    )
    assert "more than one person" in out
    assert "Ion Popescu" in out and "Ioana Popa" in out
    assert "+40 700 000 001" in out  # whole, to call or message
    assert await _outreach(sf) == []


@pytest.mark.asyncio
async def test_unknown_or_other_agents_patient_is_not_found(sf, practice):
    await _person(sf, "Vasile Other", "40700000005", agent="a2")
    out = await team.handle_team_message_person(
        {"patient": "Vasile", "text": "x", "confirmed": True},
        **_kw(sf, practice.doctor_session),
    )
    assert "nobody enrolled" in out
    assert await _outreach(sf) == []


@pytest.mark.asyncio
async def test_unconfirmed_is_a_preview_and_sends_nothing(sf, practice):
    out = await team.handle_team_message_person(
        {"patient": "Ioana", "text": "Veniți mâine la 10."},
        **_kw(sf, practice.doctor_session),
    )
    assert out.startswith("Will send to Ioana Popa (+40 700 000 002): «Veniți mâine la 10.»")
    assert _redis.ttl[next(iter(_redis.data))] == team.CONFIRM_TTL_SECONDS
    assert f'confirmation="{_code(out)}"' in out
    assert await _outreach(sf) == []


@pytest.mark.asyncio
async def test_a_yes_without_the_previews_code_only_previews(sf, practice):
    out = await team.handle_team_message_person(
        {"patient": "Ioana", "text": "x", "confirmed": True, "confirmation": "ABC123"},
        **_kw(sf, practice.doctor_session),
    )
    assert out.startswith("Not done:") and "Will send to Ioana Popa" in out
    out = await team.handle_team_message_person(
        {"patient": "Ioana", "text": "x", "confirmed": True},
        **_kw(sf, practice.doctor_session),
    )
    assert out.startswith("Not done:") and "Nothing has been sent" in out
    assert await _outreach(sf) == []


@pytest.mark.asyncio
async def test_a_code_confirms_only_the_exact_previewed_action_once_in_its_session(sf, practice):
    doctor = _kw(sf, practice.doctor_session)
    send = team.handle_team_message_person
    code = _code(await send({"patient": "Ioana", "text": "Vino la 10."}, **doctor))

    other_text = await send({"patient": "Ioana", "text": "Vino la 11.", "confirmation": code}, **doctor)
    assert other_text.startswith("Not done:") and "«Vino la 11.»" in other_text

    code = _code(await send({"patient": "Ioana", "text": "Vino la 10."}, **doctor))
    other_patient = await send({"patient": "Ion", "text": "Vino la 10.", "confirmation": code}, **doctor)
    assert other_patient.startswith("Not done:")

    code = _code(await send({"patient": "Ioana", "text": "Vino la 10."}, **doctor))
    other_tool = await team.handle_team_update_person(
        {"patient": "Ioana", "status": "ended", "reason": "x", "confirmation": code}, **doctor,
    )
    assert other_tool.startswith("Not done:")
    assert (await _enrollment(sf, practice.ioana[0])).status == "active"

    colleague, colleague_session = await _person(sf, "Asistenta Maria", "40700000098", enrol=False)
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, alert_recipient_user_ids=[practice.doctor, colleague],
    )
    other_session = await send(
        {"patient": "Ioana", "text": "Vino la 10.", "confirmation": code},
        **_kw(sf, colleague_session),
    )
    assert other_session.startswith("Not done:")
    assert await _outreach(sf) == []

    sent = await send({"patient": "Ioana", "text": "Vino la 10.", "confirmation": code}, **doctor)
    assert sent.startswith("Ioana Popa's 24h window is closed")
    replay = await send({"patient": "Ioana", "text": "Vino la 10.", "confirmation": code}, **doctor)
    assert replay.startswith("Not done:")
    assert len(await _outreach(sf)) == 1


@pytest.mark.asyncio
async def test_confirmed_queues_the_text_verbatim_like_studio(sf, practice):
    ioana, _ = practice.ioana
    out = await _confirmed(
        team.handle_team_message_person,
        {"person": "0002", "text": "  Veniți mâine la 10.  "},
        _kw(sf, practice.doctor_session),
    )
    assert "Ioana Popa's 24h window is closed: I sent the opener template" in out
    [row] = await _outreach(sf)
    assert row.user_id == ioana
    assert row.kind == "operator"
    assert row.text == "Veniți mâine la 10."
    assert (row.template_name, row.template_language) == ("msg", "ro")
    assert row.channel_identifier == "pn1"
    assert row.created_by == f"team:{practice.doctor}"


@pytest.mark.asyncio
async def test_closed_window_without_a_template_is_refused(sf, practice):
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, message_template_name=None,
    )
    out = await team.handle_team_message_person(
        {"patient": "Ioana", "text": "x", "confirmed": True},
        **_kw(sf, practice.doctor_session),
    )
    assert "24 hours" in out
    assert "+40 700 000 002" in out
    assert await _outreach(sf) == []


@pytest.mark.asyncio
async def test_an_open_window_says_the_text_is_sent_now(sf, practice):
    await store.record_inbound(
        sf, org_id=ORG, platform="whatsapp", platform_user_id="40700000002",
        channel_identifier="pn1", at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    out = await _confirmed(
        team.handle_team_message_person,
        {"patient": "Ioana", "text": "Veniți mâine."}, _kw(sf, practice.doctor_session),
    )
    assert out.startswith("Sent now:")
    [row] = await _outreach(sf)
    assert row.subject_user_id == practice.ioana[0]


@pytest.mark.asyncio
async def test_a_paused_patient_is_messaged_only_when_the_doctor_insists(sf, practice):
    ioana, _ = practice.ioana
    await store.update_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=ioana, protocol="post-op",
        status="paused", status_reason="internată",
    )
    kw = _kw(sf, practice.doctor_session)
    out = await team.handle_team_message_person(
        {"patient": "Ioana", "text": "x"}, **kw,
    )
    assert "not active (post-op paused (internată))" in out
    assert "even_if_paused" in out and "+40 700 000 002" in out
    assert await _outreach(sf) == []

    out = await _confirmed(
        team.handle_team_message_person,
        {"patient": "Ioana", "text": "x", "even_if_paused": True}, kw,
    )
    assert "window is closed" in out
    assert len(await _outreach(sf)) == 1


@pytest.mark.asyncio
async def test_no_permission_is_refused(sf, practice):
    await _person(sf, "Mihai Fără", "40700000006", permission=False)
    out = await team.handle_team_message_person(
        {"patient": "Mihai", "text": "x", "confirmed": True},
        **_kw(sf, practice.doctor_session),
    )
    assert "permission" in out
    assert "Call them instead: +40 700 000 006" in out
    assert await _outreach(sf) == []


# --- update -------------------------------------------------------------------


async def _enrollment(sf, user_id):
    [row] = await store.enrollments(sf, org_id=ORG, agent_id=AGENT, user_id=user_id)
    return row


@pytest.mark.asyncio
async def test_update_is_refused_outside_the_care_team(sf, practice):
    ioana, _ = practice.ioana
    out = await team.handle_team_update_person(
        {"patient": "Ioana", "status": "ended", "reason": "x", "confirmed": True},
        **_kw(sf, practice.ion[1]),
    )
    assert out == team.NOT_TEAM
    assert (await _enrollment(sf, ioana)).status == "active"


@pytest.mark.asyncio
async def test_update_with_an_ambiguous_patient_changes_nothing(sf, practice):
    out = await team.handle_team_update_person(
        {"patient": "pop", "status": "paused", "reason": "spital", "confirmed": True},
        **_kw(sf, practice.doctor_session),
    )
    assert "more than one person" in out and "Ion Popescu" in out and "Ioana Popa" in out
    assert {(await _enrollment(sf, u)).status for u in (practice.ion[0], practice.ioana[0])} == {"active"}


@pytest.mark.asyncio
async def test_pausing_needs_a_reason(sf, practice):
    out = await team.handle_team_update_person(
        {"patient": "Ioana", "status": "paused", "confirmed": True},
        **_kw(sf, practice.doctor_session),
    )
    assert "why" in out
    assert (await _enrollment(sf, practice.ioana[0])).status == "active"


@pytest.mark.asyncio
async def test_update_preview_changes_nothing(sf, practice):
    ioana, _ = practice.ioana
    out = await team.handle_team_update_person(
        {"patient": "Ioana", "status": "paused", "reason": "internată", "days_ago": 3,
         "answered_by": "Ana, fiica"},
        **_kw(sf, practice.doctor_session),
    )
    assert out.startswith("Will change Ioana Popa's post-op:")
    assert "status active → paused (internată)" in out
    assert "answered by none → Ana, fiica" in out
    assert "Nothing has been changed" in out
    row = await _enrollment(sf, ioana)
    assert (row.status, row.answered_by) == ("active", None)
    assert row.start_date == date.today() - timedelta(days=5)
    assert await _outreach(sf) == []


@pytest.mark.asyncio
async def test_confirmed_update_writes_and_tells_the_rest_of_the_care_team(sf, practice):
    colleague, _ = await _person(sf, "Asistenta Maria", "40700000098", enrol=False)
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT,
        alert_recipient_user_ids=[practice.doctor, colleague],
    )
    ioana, _ = practice.ioana
    out = await _confirmed(
        team.handle_team_update_person,
        {"patient": "0002", "status": "paused", "reason": "internată",
         "start_date": (date.today() - timedelta(days=2)).isoformat(),
         "start_date_approximate": True, "patient_name": "Ioana M. Popa"},
        _kw(sf, practice.doctor_session),
    )
    assert "Updated Ioana" in out and "status paused" in out
    row = await _enrollment(sf, ioana)
    assert row.status == "paused"
    assert row.status_reason == "internată (by Dr. Ionescu)"
    assert row.start_date == date.today() - timedelta(days=2)
    assert row.start_date_approximate is True
    assert row.patient_name == "Ioana M. Popa"
    [alert] = await _outreach(sf)
    assert alert.user_id == colleague  # not the doctor who did it
    assert alert.kind == "alert" and alert.subject_user_id == ioana
    assert alert.text.startswith("post-op paused by Dr. Ionescu: internată")
    assert alert.created_by == f"team:{practice.doctor}"


# --- enrol --------------------------------------------------------------------


@pytest.fixture
def bindings(monkeypatch):
    """agent_users is written with a Postgres upsert; record the call instead."""
    calls = []

    async def ensure(db, **kw):
        calls.append(kw)

    monkeypatch.setattr(team, "ensure_agent_user", ensure)
    return calls


def _enrol(**extra):
    return {"phone": "+40 711 222 333", "person_name": "Elena Dumitru", "days_ago": 2, **extra}


@pytest.mark.asyncio
async def test_enrol_is_refused_outside_the_care_team(sf, practice, bindings):
    out = await team.handle_team_enroll_person(
        _enrol(confirmed=True), **_kw(sf, practice.ion[1]),
    )
    assert out == team.NOT_TEAM
    assert bindings == []


@pytest.mark.asyncio
async def test_enrol_preview_writes_nothing(sf, practice, bindings):
    out = await team.handle_team_enroll_person(
        _enrol(answered_by="Ana, fiica"), **_kw(sf, practice.doctor_session),
    )
    assert out.startswith("Will enrol Elena Dumitru (+40 711 222 333) in post-op from ")
    assert "answered by Ana, fiica" in out and "welcome message" in out
    assert "Nothing has been changed" in out
    async with sf() as db:
        assert (await db.execute(
            sa.select(ChannelIdentity).where(ChannelIdentity.platform_user_id == "40711222333")
        )).first() is None
    assert bindings == [] and await _outreach(sf) == []


@pytest.mark.asyncio
async def test_confirmed_enrol_creates_the_phone_only_person_and_welcomes_them(sf, practice, bindings):
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, welcome_template_name="welcome",
        welcome_template_language="ro", texts={"welcome_text": "Bun venit!"},
    )
    out = await _confirmed(
        team.handle_team_enroll_person,
        _enrol(start_date_approximate=True), _kw(sf, practice.doctor_session),
    )
    assert "Enrolled Elena Dumitru (+40 711 222 333) in post-op" in out
    assert "Welcome message queued" in out
    async with sf() as db:
        identity = (await db.execute(
            sa.select(ChannelIdentity).where(ChannelIdentity.platform_user_id == "40711222333")
        )).scalar_one()
        user = await db.get(User, identity.user_id)
    assert user.email == f"whatsapp-40711222333@{ORG}.channels.surogate.local"
    assert (user.display_name, user.auth_provider) == ("Elena Dumitru", "external")
    permission = identity.platform_meta["contact_permission"]
    assert (permission["status"], permission["method"]) == ("granted", "care_team")
    assert permission["recorded_by"] == f"team:{practice.doctor}"
    [binding] = bindings
    assert binding["user_id"] == user.id and binding["agent_id"] == AGENT
    row = await _enrollment(sf, user.id)
    assert (row.enrolled_by, row.status, row.patient_name) == ("care_team", "active", "Elena Dumitru")
    assert row.start_date == date.today() - timedelta(days=2) and row.start_date_approximate
    [welcome] = await _outreach(sf)  # the doctor is the only care-team member
    assert (welcome.user_id, welcome.kind, welcome.text) == (user.id, "welcome", "Bun venit!")
    assert (welcome.template_name, welcome.template_language) == ("welcome", "ro")
    assert welcome.channel_identifier == "pn1"

    again = await _confirmed(
        team.handle_team_enroll_person, _enrol(), _kw(sf, practice.doctor_session),
    )
    assert "Enrolled" in again
    assert len(await _outreach(sf)) == 1  # the same day's welcome is not sent twice


@pytest.mark.asyncio
async def test_enrol_attaches_someone_who_already_wrote(sf, practice, bindings):
    shadow, _ = await _person(sf, "WA profile", "40711222333", enrol=False, permission=False)
    async with sf() as db:
        (await db.get(User, shadow)).auth_provider = "whatsapp"
        await db.commit()
    await _confirmed(
        team.handle_team_enroll_person, _enrol(), _kw(sf, practice.doctor_session),
    )
    async with sf() as db:
        assert (await db.get(User, shadow)).auth_provider == "external"
        assert len((await db.execute(sa.select(User))).all()) == 4  # nobody new
    assert (await _enrollment(sf, shadow)).enrolled_by == "care_team"


@pytest.mark.asyncio
async def test_a_care_team_number_is_refused(sf, practice, bindings):
    out = await team.handle_team_enroll_person(
        _enrol(phone="+40 700 000 099", confirmed=True), **_kw(sf, practice.doctor_session),
    )
    assert "member of the team" in out and out.startswith("Not enrolled")
    assert await store.enrollments(sf, org_id=ORG, agent_id=AGENT, user_id=practice.doctor) == []
    assert bindings == [] and await _outreach(sf) == []


@pytest.mark.asyncio
async def test_enrol_needs_a_date_or_date_unknown_and_a_valid_number(sf, practice, bindings):
    kw = _kw(sf, practice.doctor_session)
    no_date = await team.handle_team_enroll_person(
        {"phone": "+40 711 222 333", "patient_name": "X", "confirmed": True}, **kw,
    )
    bad = await team.handle_team_enroll_person(_enrol(phone="0711222333", confirmed=True), **kw)
    assert "days_ago" in no_date and "country code" in bad
    assert bindings == []


# --- overview memory ----------------------------------------------------------


class _Bucket:
    def __init__(self, objects):
        self.objects = objects
        self.read_keys = []

    async def read(self, bucket, key):
        assert bucket == "mem"
        self.read_keys.append(key)
        if key not in self.objects:
            raise KeyError(key)
        return encode_envelope(_MemoryEnvelope(version=1, content=self.objects[key]))


@pytest.mark.asyncio
async def test_overview_focus_includes_what_the_agent_remembers(sf, practice, monkeypatch):
    monkeypatch.setattr(team, "_memory_bucket", lambda: "mem")
    ion, ion_session = practice.ion
    ioana, ioana_session = practice.ioana
    async with sf() as db:
        for session_id, boundary in ((ion_session, "wa:ion"), (ioana_session, "wa:ioana")):
            (await db.get(SessionRow, session_id)).config = {
                "whatsapp_channel_id": "x", "memory_boundary": boundary,
            }
        db.add(SessionRow(
            id=uuid.uuid4(), org_id=ORG, agent_id=AGENT, channel="whatsapp", user_id=ion,
            config={"memory_boundary": "wa:group", "multi_party": True},
        ))
        await db.commit()
    bucket = _Bucket({
        "p/a1/boundaries/wa:ion/user.json": "Alergic la penicilină\n---\nLocuiește singur",
        "p/a1/boundaries/wa:ion/memory.json": "Alergic la penicilină\n---\n" + "x" * 5000,
        f"p/a1/users/{ion}/user.json": "Preferă dimineața",
        "p/a1/boundaries/wa:group/user.json": "group chatter",
        "p/a1/boundaries/wa:ioana/user.json": "Ioana's secret",
    })
    out = await team.handle_team_overview(
        {"patient": "popescu"}, **_kw(sf, practice.doctor_session),
        storage=bucket, tenant=SimpleNamespace(asset_root="p/a1"),
    )
    assert "what the agent remembers about them:" in out
    assert out.count("Alergic la penicilină") == 1
    assert "Locuiește singur" in out and "Preferă dimineața" in out
    assert "group chatter" not in out and "Ioana's secret" not in out
    assert "x" * 601 not in out
    assert not any("wa:ioana" in k or "wa:group" in k for k in bucket.read_keys)

    digest = await team.handle_team_overview(
        {}, **_kw(sf, practice.doctor_session),
        storage=bucket, tenant=SimpleNamespace(asset_root="p/a1"),
    )
    assert "remembers" not in digest


@pytest.mark.asyncio
async def test_overview_focus_without_memory_storage_still_answers(sf, practice):
    out = await team.handle_team_overview(
        {"patient": "popescu"}, **_kw(sf, practice.doctor_session),
    )
    assert "Ion Popescu" in out and "memory cannot be read" in out


def test_the_care_team_reads_a_whole_tappable_number_that_redaction_leaves_alone():
    """Alert footers reached the doctor as "+407****0601": the practice must call
    or message its patient, so the number is whole, spaced as WhatsApp links it."""
    from surogates.channels.identity import readable_phone
    from surogates.harness.redact import redact_sensitive_text

    assert readable_phone("40712000403") == "+40 712 000 403"
    assert readable_phone("447911123456") == "+447 911 123 456"
    footer = f"— Florica Enache · {readable_phone('40712000601')}"
    assert redact_sensitive_text(footer) == footer


@pytest.mark.asyncio
async def test_overview_lists_messages_that_did_not_arrive_as_open_items(sf, practice):
    ioana, _ = practice.ioana
    row = await store.queue_outreach(
        sf, org_id=ORG, agent_id=AGENT, user_id=ioana, platform="whatsapp",
        channel_identifier="pn1", kind="operator", text="x", template_name="msg",
        template_language="ro",
    )
    async with sf() as db:
        saved = await db.get(OutreachMessageRow, row.id)
        saved.state = "failed"
        saved.reason = "No permission to message the recipient"
        await db.commit()
    out = await team.handle_team_overview({}, **_kw(sf, practice.doctor_session))
    assert (
        "Open items, messages that did not arrive (last 72h): Ioana Popa: no "
        "permission to message them, call +40 700 000 002"
    ) in out



def test_team_tools_ask_for_a_person_and_never_speak_of_patients_or_doctors():
    for schema in (team.TEAM_OVERVIEW_SCHEMA, team.TEAM_MESSAGE_PERSON_SCHEMA,
                   team.TEAM_UPDATE_PERSON_SCHEMA, team.TEAM_ENROLL_PERSON_SCHEMA):
        text = str(schema.description) + str(schema.parameters)
        assert not re.search(r"patient|doctor|care team|practice", text, re.I), schema.name
