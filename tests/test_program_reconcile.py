"""Mirroring ops' active Programs into the runtime's schedule table."""

from __future__ import annotations

import uuid

import pytest

from surogates.programs.reconcile import reconcile_programs
from surogates.programs.store import ProgramScheduleStore


def _projected(program_id, **over):
    row = {
        "id": str(program_id),
        "org_id": str(uuid.uuid4()),
        "agent_id": "a1",
        "skill_ref": "post-op",
        "channel": "whatsapp",
        "channel_identifier": "127",
        "template_name": "daily",
        "template_language": "en_US",
        "weekdays": ["mon"],
        "times_local": ["09:00"],
        "timezone": "UTC",
        "response_deadline_hours": 24,
        "escalation_service_account_id": str(uuid.uuid4()),
        "patients": [str(uuid.uuid4())],
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_a_program_missing_from_the_projection_is_deactivated(sf):
    # This is the whole reason reconcile exists: a Program paused in ops
    # would otherwise keep firing forever.
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    await reconcile_programs(store, projected=[_projected(pid)])
    assert (await store.get(pid)).active is True

    await reconcile_programs(store, projected=[])
    assert (await store.get(pid)).active is False


@pytest.mark.asyncio
async def test_reconcile_does_not_push_a_due_program_forward(sf):
    # Reconcile runs on every publish. If it reset next_run_at each time, a
    # Program due in a minute would never fire while the operator kept
    # saving unrelated changes.
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    rows = [_projected(pid)]
    await reconcile_programs(store, projected=rows)
    first = (await store.get(pid)).next_run_at

    await reconcile_programs(store, projected=rows)
    assert (await store.get(pid)).next_run_at == first


@pytest.mark.asyncio
async def test_a_changed_cadence_does_move_the_clock(sf):
    # The other half of the rule above: when the cadence itself changes the
    # old instant is wrong and must be recomputed, or an operator's edit
    # would not take effect until the next fire.
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    await reconcile_programs(store, projected=[_projected(pid)])
    first = (await store.get(pid)).next_run_at

    await reconcile_programs(
        store, projected=[_projected(pid, weekdays=["tue"], times_local=["17:00"])],
    )
    assert (await store.get(pid)).next_run_at != first


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(sf):
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    rows = [_projected(pid)]
    await reconcile_programs(store, projected=rows)
    await reconcile_programs(store, projected=rows)
    # One schedule, not two — uq_program_schedule_program guarantees it.
    assert await store.get(pid) is not None


@pytest.mark.asyncio
async def test_the_roster_travels_on_the_schedule_config(sf):
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    patient = str(uuid.uuid4())
    await reconcile_programs(
        store, projected=[_projected(pid, patients=[patient])],
    )
    # The tick must not have to call back into ops for the roster.
    sched = await store.get(pid)
    assert sched is not None
    assert patient in sched.config["patients"]


@pytest.mark.asyncio
async def test_a_returning_program_is_reactivated(sf):
    # Pause then resume in ops. Without this the schedule stays inactive and
    # the Program silently never fires again.
    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    rows = [_projected(pid)]
    await reconcile_programs(store, projected=rows)
    await reconcile_programs(store, projected=[])
    assert (await store.get(pid)).active is False

    await reconcile_programs(store, projected=rows)
    assert (await store.get(pid)).active is True


@pytest.mark.asyncio
async def test_resuming_does_not_fire_the_slot_that_was_missed(sf):
    # A Program paused past its due time still carries that stale instant.
    # Resuming must recompute it, or every patient gets the missed check-in
    # the moment an operator un-pauses.
    from datetime import timedelta

    import sqlalchemy as sa

    from surogates.db.models import ProgramScheduleRow
    from surogates.programs.store import _utcnow

    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    rows = [_projected(pid)]
    await reconcile_programs(store, projected=rows)

    # Let the stored instant go stale while the Program sits paused, without
    # touching the cadence — a cadence change would recompute the clock by
    # itself and this test would prove nothing.
    async with sf() as db:
        await db.execute(
            sa.update(ProgramScheduleRow)
            .where(ProgramScheduleRow.program_id == pid)
            .values(next_run_at=_utcnow() - timedelta(hours=48))
        )
        await db.commit()
    await reconcile_programs(store, projected=[])
    assert (await store.get(pid)).active is False

    await reconcile_programs(store, projected=rows)
    resumed = await store.get(pid)
    assert resumed.active is True
    assert resumed.next_run_at > _utcnow()
    assert await store.claim_due(worker_id="w1", limit=10) == []
