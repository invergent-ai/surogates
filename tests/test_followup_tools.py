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
    await store.save_settings(
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
    [enrollment] = await store.enrollments_for_user(
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


@pytest.mark.parametrize("raw", ["05/10/2026", "2026-13-01", "1900-01-01"])
@pytest.mark.asyncio
async def test_enrol_refuses_a_bad_date(sf, patient, raw):
    _, session_id = patient
    result = await followup.handle_followup_enroll(
        {"protocol": "post-op", "consent": True, "summary": "x", "start_date": raw},
        **_kw(sf, session_id),
    )
    assert result.startswith("Not enrolled")


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
    assert alert.source_session_id == session_id


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
    await store.save_settings(
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
