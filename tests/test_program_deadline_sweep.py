"""Expiring check-ins nobody answered."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from surogates.programs.ticker import sweep_deadlines


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def awaiting_invitation(make_invitation):
    return await make_invitation(
        delivery_state="accepted",
        response_state="awaiting_reply",
        deadline_at=_utcnow() + timedelta(hours=24),
    )


@pytest_asyncio.fixture
async def failed_invitation(make_invitation):
    # Deadline set, never delivered.
    return await make_invitation(
        delivery_state="failed",
        response_state="awaiting_reply",
        deadline_at=_utcnow() + timedelta(hours=24),
    )


@pytest_asyncio.fixture
async def skipped_invitation(make_invitation):
    return await make_invitation(
        delivery_state="skipped",
        response_state="not_started",
        deadline_at=_utcnow() + timedelta(hours=24),
    )


@pytest_asyncio.fixture
async def replied_invitation(make_invitation):
    return await make_invitation(
        delivery_state="accepted",
        response_state="replied",
        deadline_at=_utcnow() + timedelta(hours=24),
    )


@pytest.mark.asyncio
async def test_an_unanswered_invitation_expires(
    sf, awaiting_invitation, reload_invitation,
):
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(awaiting_invitation.id)
    assert row.response_state == "no_reply_by_deadline"


@pytest.mark.asyncio
async def test_a_failed_delivery_is_not_a_non_responder(
    sf, failed_invitation, reload_invitation,
):
    # They were never asked. Counting them as silent would blame the patient
    # for our failure to reach them.
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(failed_invitation.id)
    assert row.response_state != "no_reply_by_deadline"


@pytest.mark.asyncio
async def test_a_skipped_patient_is_not_a_non_responder(
    sf, skipped_invitation, reload_invitation,
):
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(skipped_invitation.id)
    assert row.response_state != "no_reply_by_deadline"


@pytest.mark.asyncio
async def test_a_replied_invitation_is_left_alone(
    sf, replied_invitation, reload_invitation,
):
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(replied_invitation.id)
    assert row.response_state == "replied"


@pytest.mark.asyncio
async def test_the_deadline_is_not_reached_yet(
    sf, awaiting_invitation, reload_invitation,
):
    # The negative control: the sweep must key on the deadline, not simply
    # expire everything it finds awaiting a reply.
    swept = await sweep_deadlines(sf, now=_utcnow())
    assert swept == 0
    row = await reload_invitation(awaiting_invitation.id)
    assert row.response_state == "awaiting_reply"
