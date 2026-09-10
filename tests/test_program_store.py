"""Claiming and leasing due program schedules."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from surogates.programs.store import ProgramScheduleStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_claim_due_takes_the_lease(sf):
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    await store.ensure(
        program_id=pid,
        org_id=uuid.uuid4(),
        agent_id="a1",
        config={"times_local": ["09:00"]},
        next_run_at=_utcnow() - timedelta(minutes=1),
    )
    claimed = await store.claim_due(worker_id="w1", limit=10)
    assert [c.program_id for c in claimed] == [pid]

    # A second worker must not double-fire the same Program.
    assert await store.claim_due(worker_id="w2", limit=10) == []


@pytest.mark.asyncio
async def test_a_failed_tick_waits_rather_than_spinning(sf):
    # Leaving the row past-due while holding a dead lease makes claim_due
    # re-fire it forever at lease frequency.
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    await store.ensure(
        program_id=pid,
        org_id=uuid.uuid4(),
        agent_id="a1",
        config={},
        next_run_at=_utcnow() - timedelta(minutes=1),
    )
    claimed = (await store.claim_due(worker_id="w1", limit=10))[0]
    await store.mark_failed(claimed)
    assert await store.claim_due(worker_id="w2", limit=10) == []


@pytest.mark.asyncio
async def test_a_failed_tick_is_not_dead_forever(sf):
    # A Program whose cadence yields no computable next instant must still
    # come back: parking it on next_run_at=NULL would take it out of the
    # ticker permanently, and reconcile only rewrites the clock when the
    # cadence itself changes.
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    await store.ensure(
        program_id=pid,
        org_id=uuid.uuid4(),
        agent_id="a1",
        config={},
        next_run_at=_utcnow() - timedelta(minutes=1),
    )
    claimed = (await store.claim_due(worker_id="w1", limit=10))[0]
    await store.mark_failed(claimed)
    again = await store.get(pid)
    assert again.next_run_at is not None
    assert again.next_run_at > _utcnow()


@pytest.mark.asyncio
async def test_deactivate_stops_a_paused_program_firing(sf):
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    await store.ensure(
        program_id=pid,
        org_id=uuid.uuid4(),
        agent_id="a1",
        config={},
        next_run_at=_utcnow() - timedelta(minutes=1),
    )
    await store.deactivate(pid)
    assert await store.claim_due(worker_id="w1", limit=10) == []


@pytest.mark.asyncio
async def test_mark_fired_moves_the_clock_and_drops_the_lock(sf):
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    await store.ensure(
        program_id=pid,
        org_id=uuid.uuid4(),
        agent_id="a1",
        config={},
        next_run_at=_utcnow() - timedelta(minutes=1),
    )
    claimed = (await store.claim_due(worker_id="w1", limit=10))[0]
    later = _utcnow() + timedelta(hours=12)
    await store.mark_fired(claimed, next_run_at=later)

    row = await store.get(pid)
    assert row.locked_by is None
    assert row.last_run_at is not None
    # Still leased-free but not yet due, so nobody re-fires it.
    assert await store.claim_due(worker_id="w2", limit=10) == []


@pytest.mark.asyncio
async def test_deactivate_missing_stops_programs_that_left_the_projection(sf):
    store = ProgramScheduleStore(sf)
    kept, dropped = uuid.uuid4(), uuid.uuid4()
    for pid in (kept, dropped):
        await store.ensure(
            program_id=pid,
            org_id=uuid.uuid4(),
            agent_id="a1",
            config={},
            next_run_at=_utcnow() - timedelta(minutes=1),
        )
    await store.deactivate_missing({kept})

    assert (await store.get(kept)).active is True
    assert (await store.get(dropped)).active is False
    assert [c.program_id for c in await store.claim_due(worker_id="w1", limit=10)] == [
        kept
    ]
