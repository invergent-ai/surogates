"""Reproductions of the gaps the cardiology follow-up pilot hits.

A ``test_gap_*`` pins behaviour still to be fixed, so a pass means the gap
is reproduced.  A ``test_fixed_*`` is a former gap, rewritten to assert the fix.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from surogates.channels.platforms.whatsapp import _build_message
from surogates.db.models import InboxItem, ProgramInvitationRow
from surogates.programs.materialize import materialize_occurrence
from tests.test_checkin_tools import (  # noqa: F401  (fixture)
    _kwargs,
    awaiting_invitation,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_fixed_a_patient_past_the_phase_is_not_invited(sf):
    # A patient on day 40 of a protocol whose "first month" Program covers
    # days 0-30: the audience leaves them out of the occurrence entirely.
    from surogates.followup import store

    org, user = uuid.uuid4(), uuid.uuid4()
    await store.upsert_enrollment(
        sf, org_id=org, agent_id="a1", user_id=user, protocol="post-op",
        start_date=date.today() - timedelta(days=40), phase=None,
        enrolled_by="agent",
    )
    schedule = SimpleNamespace(
        program_id=uuid.uuid4(),
        org_id=org,
        agent_id="a1",
        config={
            "channel": "whatsapp",
            "channel_identifier": "pn1",
            "skill_ref": "check-in",
            "template_name": "morning",
            "template_language": "ro",
            "response_deadline_hours": 10,
            "timezone": "UTC",
            "audience": {"protocol": "post-op", "day_from": 0, "day_to": 30},
            "users": [],
        },
    )

    async def lookup(org_id, platform, user_id):
        return SimpleNamespace(
            platform_user_id="40700000001",
            platform_meta={"contact_permission": {"status": "granted"}},
        )

    occ = await materialize_occurrence(
        schedule, session_factory=sf, identity_lookup=lookup, now=_utcnow(),
    )
    async with sf() as db:
        rows = (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.occurrence_id == occ)
            )
        ).scalars().all()
    assert rows == []


async def _care_team(sf, org_id, agent_id="a1"):
    from surogates.followup import store

    doctor = uuid.uuid4()
    await store.update_settings(
        sf, org_id=org_id, agent_id=agent_id,
        escalation_service_account_id=uuid.uuid4(), channel_identifier="pn1",
        alert_recipient_user_ids=[doctor], alert_template_name="alert",
        alert_template_language="ro", message_template_name=None,
        message_template_language=None,
    )
    return doctor


@pytest.mark.asyncio
async def test_fixed_escalation_outside_a_check_in_reaches_the_care_team(sf):
    # The patient writes on their own at 2am: "am leșinat".  No check-in is
    # open, and the care team is still told — inbox and phone.
    from surogates.db.models import OutreachMessageRow, Session as SessionRow
    from surogates.tools.builtin.followup import handle_escalate

    org, session_id = uuid.uuid4(), uuid.uuid4()
    doctor = await _care_team(sf, org)
    async with sf() as db:
        db.add(SessionRow(
            id=session_id, org_id=org, agent_id="a1", channel="whatsapp",
            user_id=uuid.uuid4(),
            config={"whatsapp_channel_id": "40700000001", "channel_identifier": "pn1"},
        ))
        await db.commit()

    result = await handle_escalate(
        {"reason": "a leșinat"},
        session_id=str(session_id), session_factory=sf,
    )
    assert result.startswith("Escalated to")
    async with sf() as db:
        assert (await db.execute(sa.select(InboxItem))).scalar_one().session_id == session_id
        alert = (await db.execute(sa.select(OutreachMessageRow))).scalar_one()
    assert alert.user_id == doctor
    assert "a leșinat" in alert.text


@pytest.mark.asyncio
async def test_fixed_escalation_in_a_check_in_reaches_the_doctors_phone(sf, awaiting_invitation):
    from surogates.db.models import OutreachMessageRow
    from surogates.tools.builtin.followup import handle_escalate

    doctor = await _care_team(sf, awaiting_invitation.org_id)
    await handle_escalate(
        {"reason": "febră 38.5"}, **_kwargs(sf, awaiting_invitation),
    )
    async with sf() as db:
        assert (await db.execute(sa.select(InboxItem))).first() is not None
        alert = (await db.execute(sa.select(OutreachMessageRow))).scalar_one()
    assert alert.user_id == doctor
    assert alert.kind == "alert"
    assert alert.template_name == "alert"


def test_gap_voice_note_reaches_the_agent_with_no_text():
    # Elderly patients answer by voice. The note arrives as a file with an
    # empty prompt, and the harness tells the model it cannot transcribe.
    msg = _build_message(
        {
            "from": "40700000001",
            "id": "wamid.x",
            "type": "audio",
            "audio": {"id": "123", "mime_type": "audio/ogg", "voice": True},
        },
        names={},
        identifier="pnid",
    )
    assert msg is not None
    assert msg.text == ""  # GAP: nothing the agent can read
