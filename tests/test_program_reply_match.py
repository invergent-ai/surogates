"""Matching an inbound reply to the check-in invitation it answers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from surogates.db.models import ProgramInvitationRow, ProgramOccurrenceRow
from surogates.programs.inbound import (
    attach_reply,
    get_occurrence,
    open_invitation_for,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _seed(sf, *, agent_id="a1", response_state="awaiting_reply", org_id=None):
    org_id = org_id or uuid.uuid4()
    async with sf() as db:
        occ = ProgramOccurrenceRow(
            org_id=org_id,
            program_id=uuid.uuid4(),
            agent_id=agent_id,
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
            program_id=occ.program_id,
            org_id=org_id,
            agent_id=agent_id,
            user_id=uuid.uuid4(),
            platform="whatsapp",
            platform_user_id="40746148303",
            delivery_state="accepted",
            response_state=response_state,
            escalation_state="none",
        )
        db.add(row)
        await db.commit()
        return row


@pytest_asyncio.fixture
async def awaiting_invitation(sf):
    return await _seed(sf)


@pytest.mark.asyncio
async def test_a_reply_matches_the_open_invitation(sf, awaiting_invitation):
    found = await open_invitation_for(
        sf,
        org_id=awaiting_invitation.org_id,
        agent_id=awaiting_invitation.agent_id,
        platform="whatsapp",
        platform_user_id=awaiting_invitation.platform_user_id,
    )
    assert found is not None
    assert found.id == awaiting_invitation.id


@pytest.mark.asyncio
async def test_another_agents_invitation_is_not_matched(sf, awaiting_invitation):
    # channel_identities is org-scoped and one person can be bound to several
    # agents. Without agent_id in the key, one agent's Program would swallow
    # another agent's reply.
    found = await open_invitation_for(
        sf,
        org_id=awaiting_invitation.org_id,
        agent_id="a-different-agent",
        platform="whatsapp",
        platform_user_id=awaiting_invitation.platform_user_id,
    )
    assert found is None


@pytest.mark.asyncio
async def test_another_orgs_invitation_is_not_matched(sf, awaiting_invitation):
    found = await open_invitation_for(
        sf,
        org_id=uuid.uuid4(),
        agent_id=awaiting_invitation.agent_id,
        platform="whatsapp",
        platform_user_id=awaiting_invitation.platform_user_id,
    )
    assert found is None


@pytest.mark.asyncio
async def test_an_ordinary_message_matches_nothing(sf):
    found = await open_invitation_for(
        sf,
        org_id=uuid.uuid4(),
        agent_id="a1",
        platform="whatsapp",
        platform_user_id="40700000000",
    )
    assert found is None


@pytest.mark.asyncio
async def test_a_finished_check_in_is_not_reopened_by_a_later_message(sf):
    # Once the agent has recorded an outcome, the next message from that
    # patient is an ordinary conversation, not more of the check-in.
    await _seed(sf, response_state="completed")
    found = await open_invitation_for(
        sf,
        org_id=uuid.uuid4(),
        agent_id="a1",
        platform="whatsapp",
        platform_user_id="40746148303",
    )
    assert found is None


@pytest.mark.asyncio
async def test_attach_reply_records_the_session_and_bundle(sf, awaiting_invitation):
    # session_id is what makes the outcome tools unspoofable: they find the
    # invitation by session, so an agent cannot close a check-in it is not in.
    sid = uuid.uuid4()
    await attach_reply(
        sf, awaiting_invitation, session_id=sid, bundle_version="v7",
    )
    async with sf() as db:
        row = await db.get(ProgramInvitationRow, awaiting_invitation.id)
    assert row.response_state == "replied"
    assert row.session_id == sid
    assert row.skill_bundle_version == "v7"


@pytest.mark.asyncio
async def test_get_occurrence_carries_the_skill_reference(sf, awaiting_invitation):
    occ = await get_occurrence(sf, awaiting_invitation.occurrence_id)
    assert occ is not None
    assert occ.skill_ref == "post-op"
