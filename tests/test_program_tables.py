"""The runtime's check-in program tables."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from surogates.db.models import (
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_the_tables_create_on_sqlite(sf):
    # Portable column types are not optional: the whole runtime test suite
    # builds its schema on in-memory SQLite.
    async with sf() as db:
        sched = ProgramScheduleRow(
            program_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            agent_id="a1",
            next_run_at=_utcnow() + timedelta(minutes=5),
            active=True,
        )
        db.add(sched)
        await db.commit()


@pytest.mark.asyncio
async def test_delivery_and_response_are_independent(sf):
    # A failed send must never be able to read as a patient who did not
    # reply; keeping the axes separate is what makes that impossible.
    async with sf() as db:
        occ = ProgramOccurrenceRow(
            program_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            agent_id="a1",
            scheduled_for=_utcnow(),
            skill_ref="s",
            template_name="t",
            template_language="en_US",
            status="fired",
        )
        db.add(occ)
        await db.flush()
        inv = ProgramInvitationRow(
            occurrence_id=occ.id,
            program_id=occ.program_id,
            org_id=occ.org_id,
            agent_id="a1",
            user_id=uuid.uuid4(),
            platform="whatsapp",
            platform_user_id="40746148303",
            delivery_state="failed",
            response_state="not_started",
            escalation_state="none",
        )
        db.add(inv)
        await db.commit()
        assert inv.delivery_state == "failed"
        assert inv.response_state == "not_started"
