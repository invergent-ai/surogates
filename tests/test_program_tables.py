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
    return datetime.now(timezone.utc)


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


@pytest.mark.asyncio
async def test_datetimes_come_back_aware_utc_whatever_went_in(sf):
    # asyncpg interprets a naive datetime bound to timestamptz as SYSTEM
    # LOCAL, so a pod with TZ set would store every check-in hours off — and
    # SQLite returns naive values while Postgres returns aware ones, so the
    # same comparison raises on one backend and passes on the other. The
    # column type pins both ends: naive in means UTC, and reads are always
    # aware UTC.
    from datetime import timedelta, timezone as tz

    naive = datetime(2026, 9, 9, 9, 0)
    bucharest = tz(timedelta(hours=3))
    aware_local = datetime(2026, 9, 9, 12, 0, tzinfo=bucharest)  # same instant

    async with sf() as db:
        a = ProgramScheduleRow(
            program_id=uuid.uuid4(), org_id=uuid.uuid4(), agent_id="a1",
            next_run_at=naive, active=True,
        )
        b = ProgramScheduleRow(
            program_id=uuid.uuid4(), org_id=uuid.uuid4(), agent_id="a1",
            next_run_at=aware_local, active=True,
        )
        db.add_all([a, b])
        await db.commit()
        a_id, b_id = a.id, b.id

    async with sf() as db:
        ra = await db.get(ProgramScheduleRow, a_id)
        rb = await db.get(ProgramScheduleRow, b_id)

    assert ra.next_run_at.tzinfo is not None
    assert ra.next_run_at.utcoffset() == timedelta(0)
    # The naive value was taken as UTC, not shifted by any local offset.
    assert ra.next_run_at == datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
    # The aware non-UTC value was normalised to the same instant in UTC.
    assert rb.next_run_at == ra.next_run_at
