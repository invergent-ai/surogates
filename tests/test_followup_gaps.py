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
from surogates.db.models import DeliveryOutbox, InboxItem, ProgramInvitationRow
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


@pytest.mark.asyncio
async def test_gap_escalation_outside_a_check_in_is_refused(sf, awaiting_invitation):
    # The patient writes on their own at 2am: "am leșinat". There is no open
    # check-in in that conversation, so nobody can be told.
    from surogates.tools.builtin.checkin import handle_checkin_escalate

    kwargs = _kwargs(sf, awaiting_invitation)
    kwargs["session_id"] = uuid.uuid4()
    result = await handle_checkin_escalate({"reason": "a leșinat"}, **kwargs)
    assert "no check-in is open" in result.lower()  # GAP
    async with sf() as db:
        assert (await db.execute(sa.select(InboxItem))).first() is None


@pytest.mark.asyncio
async def test_gap_escalation_never_reaches_the_doctors_phone(sf, awaiting_invitation):
    # Inside a check-in the escalation lands in a Studio inbox and nowhere
    # else: nothing is queued for delivery to anyone's WhatsApp.
    from surogates.tools.builtin.checkin import handle_checkin_escalate

    await handle_checkin_escalate(
        {"reason": "febră 38.5"}, **_kwargs(sf, awaiting_invitation),
    )
    async with sf() as db:
        assert (await db.execute(sa.select(InboxItem))).first() is not None
        assert (await db.execute(sa.select(DeliveryOutbox))).first() is None  # GAP


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
