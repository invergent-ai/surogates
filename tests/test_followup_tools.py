"""followup_enroll / followup_update / followup_status / escalate."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa

from surogates.db.models import (
    ChannelIdentity,
    InboxItem,
    OutreachMessageRow,
    ProgramScheduleRow,
    Session as SessionRow,
    User,
)
from surogates.followup import store
from surogates.tools.builtin import followup

ORG = uuid.uuid4()
AGENT = "a1"
PHONE = "40700000001"


@pytest_asyncio.fixture
async def patient(sf):
    """A person in their WhatsApp conversation with the agent."""
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        db.add(User(id=user_id, org_id=ORG, email=f"{user_id}@x", display_name="Ion Popescu"))
        db.add(ChannelIdentity(
            org_id=ORG, user_id=user_id, platform="whatsapp",
            platform_user_id=PHONE, platform_meta={"display_name": "Ion"},
        ))
        db.add(SessionRow(
            id=session_id, org_id=ORG, agent_id=AGENT, channel="whatsapp",
            user_id=user_id,
            config={"whatsapp_channel_id": PHONE, "channel_identifier": "pn1"},
        ))
        db.add(ProgramScheduleRow(
            program_id=uuid.uuid4(), org_id=ORG, agent_id=AGENT, active=True,
            config={"audience": {"protocol": "post-op", "day_to": 30},
                    "timezone": "Europe/Bucharest"},
        ))
        await db.commit()
    return user_id, session_id


@pytest_asyncio.fixture
async def care_team(sf):
    doctor, inbox_sa = uuid.uuid4(), uuid.uuid4()
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, escalation_service_account_id=inbox_sa,
        channel_identifier="pn1", alert_recipient_user_ids=[doctor],
        alert_template_name="alert", alert_template_language="ro",
        message_template_name=None, message_template_language=None,
    )
    return doctor, inbox_sa


def _kw(sf, session_id):
    return {"session_id": str(session_id), "session_factory": sf}


async def _outreach(sf):
    async with sf() as db:
        return (await db.execute(sa.select(OutreachMessageRow))).scalars().all()


async def _permission(sf):
    async with sf() as db:
        ident = (await db.execute(sa.select(ChannelIdentity))).scalar_one()
    return ident.platform_meta


# --- enrol ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrol_records_consent_and_tells_the_care_team(sf, patient, care_team):
    user_id, session_id = patient
    doctor, _ = care_team
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "start_date": "2026-10-05", "consent": True,
         "summary": "Pacient nou, procedură pe 5 octombrie."},
        **_kw(sf, session_id),
    )
    assert "Enrolled in post-op from 2026-10-05" in result
    assert "notified (1)" in result
    [enrollment] = await store.enrollments(
        sf, org_id=ORG, agent_id=AGENT, user_id=user_id,
    )
    assert enrollment.start_date == date(2026, 10, 5)
    assert enrollment.enrolled_by == "agent"
    meta = await _permission(sf)
    assert meta["contact_permission"]["status"] == "granted"
    assert meta["contact_permission"]["method"] == "consent"
    assert meta["display_name"] == "Ion"  # merged, not replaced
    [note] = await _outreach(sf)
    assert note.user_id == doctor
    assert note.kind == "enrolment"
    assert "Ion Popescu · +40700000001" in note.text


@pytest.mark.asyncio
async def test_enrol_without_consent_does_nothing(sf, patient, care_team):
    _, session_id = patient
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": False, "summary": "x"},
        **_kw(sf, session_id),
    )
    assert "consent is required" in result
    assert "contact_permission" not in await _permission(sf)
    assert await _outreach(sf) == []


@pytest.mark.asyncio
async def test_enrol_refuses_a_protocol_no_program_offers(sf, patient):
    _, session_id = patient
    result = await followup.handle_followup_enroll(
        {"protocol": "made-up", "consent": True, "summary": "x"},
        **_kw(sf, session_id),
    )
    assert "not offered" in result
    assert "post-op" in result


async def _deactivate_programs(sf):
    async with sf() as db:
        await db.execute(sa.update(ProgramScheduleRow).values(active=False))
        await db.commit()


@pytest.mark.asyncio
async def test_no_active_program_is_named_a_practice_configuration_problem(sf, patient):
    # "Offered: none" read to the model as the patient's fault, so it told
    # them something inaccurate and escalated with the wrong reason.
    _, session_id = patient
    await _deactivate_programs(sf)
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x"},
        **_kw(sf, session_id),
    )
    assert result.startswith("Not enrolled")
    assert "no active Program" in result
    assert "configuration problem at the practice" in result
    assert "escalate" in result
    assert "post-op" in result

    status = await followup.handle_followup_status({}, **_kw(sf, session_id))
    assert status.startswith("Protocols offered: none.")
    assert "configuration problem at the practice" in status
    assert "escalate" in status


@pytest.mark.parametrize("raw", ["05/10/2026", "2026-13-01", "1900-01-01"])
@pytest.mark.asyncio
async def test_enrol_refuses_a_bad_date(sf, patient, raw):
    _, session_id = patient
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x", "start_date": raw},
        **_kw(sf, session_id),
    )
    assert result.startswith("Not enrolled")


@pytest.mark.parametrize("days_ago", [5, "5"])
@pytest.mark.asyncio
async def test_enrol_counts_days_ago_in_the_protocols_timezone(sf, patient, days_ago):
    # "acum 5 zile": the model reached for the terminal to do the arithmetic.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    user_id, session_id = patient
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x",
         "start_date": "", "days_ago": days_ago},
        **_kw(sf, session_id),
    )
    expected = datetime.now(ZoneInfo("Europe/Bucharest")).date() - timedelta(days=5)
    assert f"from {expected.isoformat()}" in result
    [enrollment] = await store.enrollments(sf, org_id=ORG, agent_id=AGENT, user_id=user_id)
    assert enrollment.start_date == expected


@pytest.mark.parametrize("arguments, error", [
    ({"start_date": "2026-10-05", "days_ago": 5}, "not both"),
    ({"days_ago": -3}, "whole number"),
    ({"days_ago": True}, "whole number"),
    ({"days_ago": 4000}, "too far"),
])
@pytest.mark.asyncio
async def test_enrol_refuses_a_bad_days_ago(sf, patient, arguments, error):
    _, session_id = patient
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x", **arguments},
        **_kw(sf, session_id),
    )
    assert result.startswith("Not enrolled")
    assert error in result


@pytest.mark.asyncio
async def test_enrol_only_in_a_whatsapp_conversation(sf, patient):
    _, session_id = patient
    async with sf() as db:
        row = await db.get(SessionRow, session_id)
        row.channel = "web"
        await db.commit()
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x"},
        **_kw(sf, session_id),
    )
    assert "WhatsApp conversation" in result


@pytest.mark.asyncio
async def test_enrol_without_a_care_team_still_enrols_and_says_so(sf, patient):
    _, session_id = patient
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x"},
        **_kw(sf, session_id),
    )
    assert "Enrolled in post-op" in result
    assert "not notified: no care-team phones are configured" in result


# --- update -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_pausing_needs_a_reason_and_tells_the_care_team(sf, patient, care_team):
    user_id, session_id = patient
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user_id, protocol="post-op",
        start_date=None, phase=None, enrolled_by="agent",
    )
    refused = await followup.handle_followup_update(
        {"protocol": "post-op", "status": "paused"}, **_kw(sf, session_id),
    )
    assert "say why" in refused
    result = await followup.handle_followup_update(
        {"protocol": "post-op", "status": "paused", "reason": "internat"},
        **_kw(sf, session_id),
    )
    assert "status paused" in result
    [note] = await _outreach(sf)
    assert "post-op paused: internat" in note.text


@pytest.mark.asyncio
async def test_update_moves_the_phase_without_notifying(sf, patient, care_team):
    user_id, session_id = patient
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user_id, protocol="post-op",
        start_date=date(2026, 10, 1), phase="titration", enrolled_by="agent",
    )
    result = await followup.handle_followup_update(
        {"protocol": "post-op", "phase": "maintenance"}, **_kw(sf, session_id),
    )
    assert "phase maintenance" in result
    assert "start 2026-10-01" in result
    assert await _outreach(sf) == []


@pytest.mark.asyncio
async def test_blank_fields_never_erase_what_is_recorded(sf, patient, care_team):
    user_id, session_id = patient
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user_id, protocol="post-op",
        start_date=date(2026, 10, 1), phase="acute", enrolled_by="agent",
    )
    result = await followup.handle_followup_update(
        {"protocol": "post-op", "status": "paused", "reason": "internat",
         "start_date": "", "phase": ""},
        **_kw(sf, session_id),
    )
    assert "status paused" in result
    [row] = await store.enrollments(
        sf, org_id=ORG, agent_id=AGENT, user_id=user_id,
    )
    assert row.start_date == date(2026, 10, 1)
    assert row.phase == "acute"


@pytest.mark.asyncio
async def test_update_of_someone_not_enrolled(sf, patient):
    _, session_id = patient
    result = await followup.handle_followup_update(
        {"protocol": "post-op", "phase": "x"}, **_kw(sf, session_id),
    )
    assert "not enrolled" in result


# --- status -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_lists_offers_and_the_day(sf, patient):
    user_id, session_id = patient
    before = await followup.handle_followup_status({}, **_kw(sf, session_id))
    assert "Protocols offered: post-op." in before
    assert "not enrolled" in before
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/Bucharest")).date()
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user_id, protocol="post-op",
        start_date=today - timedelta(days=12), phase="acute", enrolled_by="agent",
    )
    after = await followup.handle_followup_status({}, **_kw(sf, session_id))
    assert "post-op: active" in after
    assert "(day 12 today)" in after
    assert "phase acute" in after


# --- escalate ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_reaches_inbox_and_phone(sf, patient, care_team):
    _, session_id = patient
    doctor, inbox_sa = care_team
    result = await followup.handle_escalate(
        {"reason": "Spune că a leșinat azi-dimineață."}, **_kw(sf, session_id),
    )
    assert result == (
        "Escalated to the responsible operator's inbox and 1 care-team phone(s)."
    )
    async with sf() as db:
        item = (await db.execute(sa.select(InboxItem))).scalar_one()
    assert item.service_account_id == inbox_sa
    assert item.title == "Needs your attention: Ion Popescu · +40700000001"
    [alert] = await _outreach(sf)
    assert alert.user_id == doctor


@pytest.mark.asyncio
async def test_escalate_with_nobody_configured_fails_visibly(sf, patient):
    _, session_id = patient
    result = await followup.handle_escalate(
        {"reason": "durere în piept"}, **_kw(sf, session_id),
    )
    assert result.startswith("Could not escalate")
    assert "contact the practice directly" in result


@pytest.mark.asyncio
async def test_the_care_team_member_is_not_alerted_about_themselves(sf, patient):
    user_id, session_id = patient
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, escalation_service_account_id=None,
        channel_identifier="pn1", alert_recipient_user_ids=[user_id],
        alert_template_name=None, alert_template_language=None,
        message_template_name=None, message_template_language=None,
    )
    result = await followup.handle_escalate({"reason": "x"}, **_kw(sf, session_id))
    assert result.startswith("Could not escalate")
    assert await _outreach(sf) == []


def test_every_registered_tool_runs_in_the_harness():
    # An unlisted tool defaults to the sandbox, which has no database: the
    # tools would fail on every call in production.
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    registry = ToolRegistry()
    followup.register(registry)
    for name in followup.FOLLOWUP_TOOL_NAMES:
        assert registry.has(name)
        assert TOOL_LOCATIONS[name] is ToolLocation.HARNESS


@pytest.mark.asyncio
async def test_the_care_team_is_recognised_and_never_enrolled(sf, patient):
    user_id, session_id = patient
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, escalation_service_account_id=None,
        channel_identifier="pn1", alert_recipient_user_ids=[user_id],
        alert_template_name=None, alert_template_language=None,
        message_template_name=None, message_template_language=None,
    )
    status = await followup.handle_followup_status({}, **_kw(sf, session_id))
    assert "on the care team" in status
    refused = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x"},
        **_kw(sf, session_id),
    )
    assert "on the care team" in refused
    assert await store.enrollments(
        sf, org_id=ORG, agent_id=AGENT, user_id=user_id,
    ) == []


# --- patient identity -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_relative_enrols_the_patient_by_name(sf, patient, care_team):
    # The WhatsApp profile is the daughter's: alerts named her, never him.
    _, session_id = patient
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x",
         "patient_name": " Petre Marin ", "answered_by": "Ana, fiica"},
        **_kw(sf, session_id),
    )
    assert "for Petre Marin (answered by Ana, fiica)" in result
    [note] = await _outreach(sf)
    assert note.text.endswith("— Petre Marin · via Ana, fiica · +40700000001")

    status = await followup.handle_followup_status({}, **_kw(sf, session_id))
    assert "patient Petre Marin, answered by Ana, fiica" in status

    await followup.handle_escalate({"reason": "durere"}, **_kw(sf, session_id))
    async with sf() as db:
        item = (await db.execute(sa.select(InboxItem))).scalar_one()
    assert item.title == (
        "Needs your attention: Petre Marin · via Ana, fiica · +40700000001"
    )


@pytest.mark.asyncio
async def test_update_names_the_patient_and_reenrolling_keeps_the_name(sf, patient):
    user_id, session_id = patient
    await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user_id, protocol="post-op",
        start_date=None, phase=None, enrolled_by="agent",
    )
    status = await followup.handle_followup_status({}, **_kw(sf, session_id))
    assert "patient's name not recorded" in status
    result = await followup.handle_followup_update(
        {"protocol": "post-op", "patient_name": "Petre Marin",
         "answered_by": "Ana, fiica"},
        **_kw(sf, session_id),
    )
    assert "patient Petre Marin, answered by Ana, fiica" in result
    row = await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user_id, protocol="post-op",
        start_date=None, phase=None, enrolled_by="agent",
    )
    assert (row.patient_name, row.answered_by) == ("Petre Marin", "Ana, fiica")


def test_patient_label_prefers_the_enrollment_over_the_profile():
    from types import SimpleNamespace

    assert store.patient_label("Ana (fiica)", None) == "Ana (fiica)"
    assert store.patient_label(
        "Ana (fiica)", SimpleNamespace(patient_name="Petre Marin", answered_by=None),
    ) == "Petre Marin"


@pytest.mark.asyncio
async def test_a_rough_period_is_enrolled_as_approximate(sf, patient):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    user_id, session_id = patient
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x",
         "days_ago": 14, "start_date_approximate": True},
        **_kw(sf, session_id),
    )
    expected = datetime.now(ZoneInfo("Europe/Bucharest")).date() - timedelta(days=14)
    assert f"from about {expected.isoformat()} (approximate" in result
    status = await followup.handle_followup_status({}, **_kw(sf, session_id))
    assert f"start about {expected.isoformat()} (approximate" in status

    corrected = await followup.handle_followup_update(
        {"protocol": "post-op", "start_date": expected.isoformat()},
        **_kw(sf, session_id),
    )
    assert "approximate" not in corrected
    [row] = await store.enrollments(sf, org_id=ORG, agent_id=AGENT, user_id=user_id)
    assert row.start_date_approximate is False


@pytest.mark.asyncio
async def test_approximate_without_a_start_date_is_not_recorded(sf, patient):
    user_id, session_id = patient
    await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x",
         "start_date_approximate": True},
        **_kw(sf, session_id),
    )
    [row] = await store.enrollments(sf, org_id=ORG, agent_id=AGENT, user_id=user_id)
    assert row.start_date is None
    assert row.start_date_approximate is False


# --- next scheduled contact -------------------------------------------------


async def _morning_program(sf):
    async with sf() as db:
        await db.execute(sa.update(ProgramScheduleRow).values(config={
            "name": "Prima lună", "timezone": "Europe/Bucharest",
            "weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "times_local": ["08:30"],
            "audience": {"protocol": "post-op", "day_from": 0, "day_to": 30},
        }))
        await db.commit()


@pytest.mark.asyncio
async def test_enrol_and_status_say_when_the_next_message_comes(sf, patient):
    _, session_id = patient
    await _morning_program(sf)
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x", "days_ago": 2},
        **_kw(sf, session_id),
    )
    assert "Next scheduled message: " in result
    assert "08:30 Europe/Bucharest (Program 'Prima lună')" in result
    status = await followup.handle_followup_status({}, **_kw(sf, session_id))
    assert "08:30 Europe/Bucharest (Program 'Prima lună')" in status


@pytest.mark.asyncio
async def test_someone_past_every_program_is_promised_no_time(sf, patient):
    _, session_id = patient
    await _morning_program(sf)
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x", "days_ago": 40},
        **_kw(sf, session_id),
    )
    assert "No scheduled message ahead" in result
    assert "Do not promise them a time" in result
    paused = await followup.handle_followup_update(
        {"protocol": "post-op", "status": "paused", "reason": "internat"},
        **_kw(sf, session_id),
    )
    assert "status paused" in paused
    status = await followup.handle_followup_status({}, **_kw(sf, session_id))
    assert "No scheduled message: the enrolment is paused." in status
