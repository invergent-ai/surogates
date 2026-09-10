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
async def test_deactivate_withdraws_openers_not_yet_handed_over(
    sf, make_invitation, reload_invitation,
):
    # Stopping the schedule is not enough: the send pass picks up every
    # queued opener whoever it belongs to, so yesterday's unsent opener would
    # still go out after the operator paused the Program.
    store = ProgramScheduleStore(sf)
    unsent = await make_invitation(delivery_state="queued", response_state="not_started")
    handed_over = await make_invitation(
        delivery_state="queued", response_state="awaiting_reply", outbox_id=7,
    )

    await store.deactivate(unsent.program_id)
    await store.deactivate(handed_over.program_id)

    row = await reload_invitation(unsent.id)
    assert row.delivery_state == "canceled"
    assert row.reason
    # Already in the outbox: the dispatcher owns it now, and its verdict
    # is what the history should show.
    assert (await reload_invitation(handed_over.id)).delivery_state == "queued"


@pytest.mark.asyncio
async def test_deactivate_missing_withdraws_the_unsent_openers_too(
    sf, make_invitation, reload_invitation,
):
    store = ProgramScheduleStore(sf)
    unsent = await make_invitation(delivery_state="queued", response_state="not_started")
    await store.ensure(
        program_id=unsent.program_id,
        org_id=unsent.org_id,
        agent_id="a1",
        config={},
        next_run_at=_utcnow() - timedelta(minutes=1),
    )
    await store.deactivate_missing(set())
    assert (await reload_invitation(unsent.id)).delivery_state == "canceled"


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


@pytest.mark.asyncio
async def test_an_unchanged_reconcile_writes_nothing(sf):
    # Reconcile runs every tick. Rewriting an identical config for every
    # Program each time is a write per Program per tick for nothing.
    from surogates.db.models import ProgramScheduleRow
    import sqlalchemy as sa

    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    cfg = {"weekdays": ["mon"], "times_local": ["09:00"], "timezone": "UTC"}
    due = _utcnow() + timedelta(hours=1)
    await store.ensure(program_id=pid, org_id=uuid.uuid4(), agent_id="a1", config=cfg, next_run_at=due)

    # Plant a marker the no-op path must not disturb.
    async with sf() as db:
        await db.execute(
            sa.update(ProgramScheduleRow)
            .where(ProgramScheduleRow.program_id == pid)
            .values(locked_by="marker")
        )
        await db.commit()

    again = await store.ensure(
        program_id=pid, org_id=(await store.get(pid)).org_id, agent_id="a1",
        config=dict(cfg), next_run_at=_utcnow() + timedelta(days=3),
    )
    assert again.locked_by == "marker"
    assert again.next_run_at == due


@pytest.mark.asyncio
async def test_a_reordered_weekday_list_does_not_move_the_clock(sf):
    # Reconcile runs before claim_due. If an unstable ordering from ops were
    # read as a cadence change, the tick in which `now` first crossed the due
    # instant would move the clock past it before the claim ran — and the
    # Program would never fire.
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    org = uuid.uuid4()
    due = _utcnow() + timedelta(hours=1)
    await store.ensure(
        program_id=pid, org_id=org, agent_id="a1",
        config={"weekdays": ["mon", "wed"], "times_local": ["09:00", "21:00"], "timezone": "UTC"},
        next_run_at=due,
    )
    after = await store.ensure(
        program_id=pid, org_id=org, agent_id="a1",
        config={"weekdays": ["wed", "mon"], "times_local": ["21:00", "09:00"], "timezone": "UTC"},
        next_run_at=_utcnow() + timedelta(days=3),
    )
    assert after.next_run_at == due
