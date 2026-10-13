"""care_team_overview: the care team's view across patients."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from surogates.db.models import (
    ChannelIdentity,
    Event,
    ProgramInvitationRow,
    ProgramScheduleRow,
    Session as SessionRow,
    User,
)
from surogates.followup import store
from surogates.tools.builtin import care_team

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


def _kw(sf, session_id):
    return {"session_id": str(session_id), "session_factory": sf}


# --- gating -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_patient_session_is_refused(sf, practice):
    _, ion_session = practice.ion
    overview = await care_team.handle_care_team_overview({}, **_kw(sf, ion_session))
    assert overview == care_team.NOT_CARE_TEAM


@pytest.mark.asyncio
async def test_tools_are_offered_only_to_the_care_team_with_programs_on(sf, practice):
    async with sf() as db:
        doctor = await db.get(SessionRow, practice.doctor_session)
        patient = await db.get(SessionRow, practice.ion[1])

    async def excluded(session, programs):
        return await care_team.care_team_tool_exclusions(
            sf, session, programs_enabled=programs,
        )

    assert await excluded(doctor, True) == frozenset()
    assert await excluded(doctor, False) == care_team.CARE_TEAM_TOOL_NAMES
    assert await excluded(patient, True) == care_team.CARE_TEAM_TOOL_NAMES
    assert await excluded(SimpleNamespace(user_id=None), True) == care_team.CARE_TEAM_TOOL_NAMES


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
            text="Durere în piept\n\n— Ion Popescu · +40700000001",
            template_name=None, template_language=None, subject_user_id=ion,
        )
    await store.record_inbound(
        sf, org_id=ORG, platform="whatsapp", platform_user_id="40700000002",
        channel_identifier="pn1", at=now - timedelta(days=4),
    )

    out = await care_team.handle_care_team_overview(
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

    out = await care_team.handle_care_team_overview(
        {"patient": "popescu"}, **_kw(sf, practice.doctor_session),
    )
    assert "Ioana" not in out
    assert "them: Mă doare capul" in out
    assert "agent: Am notat." in out
    assert "[Check-in]" not in out
