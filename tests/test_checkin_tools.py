"""The two tools an agent uses to close or escalate a check-in."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa

from surogates.db.models import (
    InboxItem,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
)

ESCALATION_SA = uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _reload(sf, invitation_id):
    async with sf() as db:
        return await db.get(ProgramInvitationRow, invitation_id)


async def _latest_inbox_item(sf):
    async with sf() as db:
        return (
            await db.execute(
                sa.select(InboxItem).order_by(InboxItem.id.desc()).limit(1)
            )
        ).scalar_one_or_none()


@pytest_asyncio.fixture
async def awaiting_invitation(sf):
    """A check-in the patient replied to, attached to their session."""
    program_id, org_id = uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        db.add(
            ProgramScheduleRow(
                program_id=program_id,
                org_id=org_id,
                agent_id="a1",
                active=True,
                config={"escalation_service_account_id": str(ESCALATION_SA)},
            )
        )
        occ = ProgramOccurrenceRow(
            program_id=program_id,
            org_id=org_id,
            agent_id="a1",
            scheduled_for=_utcnow(),
            skill_ref="post-op",
            template_name="daily",
            template_language="en_US",
            status="fired",
        )
        db.add(occ)
        await db.flush()
        row = ProgramInvitationRow(
            occurrence_id=occ.id,
            program_id=program_id,
            org_id=org_id,
            agent_id="a1",
            user_id=uuid.uuid4(),
            platform="whatsapp",
            platform_user_id="40746148303",
            delivery_state="accepted",
            response_state="replied",
            escalation_state="none",
            session_id=uuid.uuid4(),
        )
        db.add(row)
        await db.commit()
        return row


def _kwargs(sf, invitation):
    # Exactly what tools.dispatch passes its handlers.
    return {
        "session_id": invitation.session_id,
        "session_factory": sf,
        "tenant": None,
        "api_client": None,
        "session_config": {},
    }


@pytest.mark.asyncio
async def test_checkin_outcome_records_completion(sf, awaiting_invitation):
    from surogates.tools.builtin.checkin import handle_checkin_outcome

    await handle_checkin_outcome(
        {"outcome": "completed"}, **_kwargs(sf, awaiting_invitation),
    )
    assert (await _reload(sf, awaiting_invitation.id)).response_state == "completed"


@pytest.mark.asyncio
async def test_checkin_outcome_rejects_an_unknown_outcome(sf, awaiting_invitation):
    from surogates.tools.builtin.checkin import handle_checkin_outcome

    result = await handle_checkin_outcome(
        {"outcome": "maybe"}, **_kwargs(sf, awaiting_invitation),
    )
    assert "completed" in result
    assert (await _reload(sf, awaiting_invitation.id)).response_state == "replied"


@pytest.mark.asyncio
async def test_checkin_outcome_outside_a_check_in_writes_nothing(
    sf, awaiting_invitation,
):
    # The invitation is found by session, so an agent in an ordinary
    # conversation cannot close a check-in it is not part of.
    from surogates.tools.builtin.checkin import handle_checkin_outcome

    kwargs = _kwargs(sf, awaiting_invitation)
    kwargs["session_id"] = uuid.uuid4()
    result = await handle_checkin_outcome({"outcome": "completed"}, **kwargs)
    assert "no check-in" in result.lower()
    assert (await _reload(sf, awaiting_invitation.id)).response_state == "replied"


@pytest.mark.asyncio
async def test_escalation_targets_the_operator_not_the_patient(
    sf, awaiting_invitation,
):
    # An item created against the acting principal lands in the patient's own
    # inbox and reaches nobody else.
    from surogates.tools.builtin.checkin import handle_checkin_escalate

    await handle_checkin_escalate(
        {"reason": "BP 180/110"}, **_kwargs(sf, awaiting_invitation),
    )
    item = await _latest_inbox_item(sf)
    assert item is not None
    assert item.service_account_id == ESCALATION_SA
    assert item.user_id is None
    # Must be an existing InboxKind — the ops Inbox filters on a Literal, and
    # a new value would simply never be listed.
    assert item.kind == "action_required"
    assert item.session_id == awaiting_invitation.session_id
    assert (await _reload(sf, awaiting_invitation.id)).escalation_state == "raised"


@pytest.mark.asyncio
async def test_a_failed_escalation_is_visible(sf, awaiting_invitation):
    # The agent saying it escalated is not evidence anyone was told.
    from surogates.tools.builtin.checkin import handle_checkin_escalate

    async with sf() as db:
        sched = (
            await db.execute(
                sa.select(ProgramScheduleRow).where(
                    ProgramScheduleRow.program_id == awaiting_invitation.program_id
                )
            )
        ).scalar_one()
        sched.config = {}  # no escalation target configured
        await db.commit()

    result = await handle_checkin_escalate(
        {"reason": "x"}, **_kwargs(sf, awaiting_invitation),
    )
    row = await _reload(sf, awaiting_invitation.id)
    assert row.escalation_state == "failed"
    assert row.reason
    assert "could not escalate" in result.lower()
    assert await _latest_inbox_item(sf) is None


@pytest.mark.asyncio
async def test_escalation_does_not_close_the_check_in(sf, awaiting_invitation):
    # Escalation and response are separate axes: raising a hand to the doctor
    # does not mean the patient finished answering.
    from surogates.tools.builtin.checkin import handle_checkin_escalate

    await handle_checkin_escalate(
        {"reason": "BP 180/110"}, **_kwargs(sf, awaiting_invitation),
    )
    assert (await _reload(sf, awaiting_invitation.id)).response_state == "replied"
