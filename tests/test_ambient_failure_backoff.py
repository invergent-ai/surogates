"""A failed ambient tick must advance its clock, not spin.

``materialize_ambient_tick`` calls ``mark_fired`` last, after
``enqueue_session``. Anything that raises above it leaves ``next_run_at`` in
the past and the row still ``locked_by`` a worker that has already given up.
``claim_due`` only requires ``locked_until <= now``, so once the 120s lease
lapses the row is claimed again -- and again, forever, at whatever rate the
lease allows, with no backoff and no ceiling.

The ticker already isolates a bad row so its neighbours still run; what it
never did was tell the store the attempt failed.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from surogates.ambient.store import AmbientSchedule, AmbientScheduleStore
from surogates.ambient.ticker import AmbientTicker
from surogates.db.models import AmbientScheduleRow


# ---------------------------------------------------------------------------
# Ticker: a failed row must be reported to the store
# ---------------------------------------------------------------------------


class _RecordingStore:
    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.claims = 0
        self.failed: list = []

    async def claim_due(self, *, worker_id, limit, lease_seconds=120):
        self.claims += 1
        return self._rows if self.claims == 1 else []

    async def mark_failed(self, schedule) -> None:
        self.failed.append(schedule)


def _row(channel: str = "C1") -> AmbientSchedule:
    return AmbientSchedule(
        id=uuid.uuid4(), org_id=uuid.uuid4(), agent_id="ag", platform="slack",
        channel_id=channel, cadence_seconds=1800, status="active",
    )


@pytest.mark.asyncio
async def test_failed_materialize_marks_the_schedule_failed():
    rows = [_row("C1")]
    store = _RecordingStore(rows)

    async def boom(_row):
        raise RuntimeError("materialize exploded")

    ticker = AmbientTicker(
        store, redis=None, materialize=boom, worker_id="w1",
    )
    await ticker.tick_once()

    assert [s.channel_id for s in store.failed] == ["C1"], (
        "a failed tick must advance its own clock, or it is re-claimed "
        "as soon as the lease lapses, forever"
    )


@pytest.mark.asyncio
async def test_successful_materialize_does_not_mark_failed():
    store = _RecordingStore([_row("C1")])

    async def ok(_row):
        return None

    ticker = AmbientTicker(store, redis=None, materialize=ok, worker_id="w1")
    await ticker.tick_once()

    assert store.failed == []


@pytest.mark.asyncio
async def test_one_failure_still_marks_only_that_row():
    """Neighbours keep running and are not penalised for a sibling's failure."""
    store = _RecordingStore([_row("C1"), _row("C2")])
    seen: list[str] = []

    async def first_fails(row):
        seen.append(row.channel_id)
        if row.channel_id == "C1":
            raise RuntimeError("boom")

    ticker = AmbientTicker(
        store, redis=None, materialize=first_fails, worker_id="w1",
    )
    await ticker.tick_once()

    assert seen == ["C1", "C2"]
    assert [s.channel_id for s in store.failed] == ["C1"]


@pytest.mark.asyncio
async def test_a_failing_mark_failed_does_not_abort_the_pass():
    """If the bookkeeping write itself fails, later rows must still run."""
    store = _RecordingStore([_row("C1"), _row("C2")])

    async def always_fails(_row):
        raise RuntimeError("boom")

    async def broken_mark_failed(_schedule):
        raise RuntimeError("db blip")

    store.mark_failed = broken_mark_failed  # type: ignore[assignment]

    ticker = AmbientTicker(
        store, redis=None, materialize=always_fails, worker_id="w1",
    )
    await ticker.tick_once()  # must not raise


# ---------------------------------------------------------------------------
# Store: mark_failed advances the clock and drops the lock
# ---------------------------------------------------------------------------


@pytest.fixture
async def store():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(AmbientScheduleRow.__table__.create)
    yield AmbientScheduleStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


async def _make_due(store) -> None:
    """``ensure`` schedules the first run one cadence out; pull it into the past."""
    async with store._sf() as db:
        await db.execute(
            sa.update(AmbientScheduleRow).values(
                next_run_at=dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(seconds=1),
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_mark_failed_pushes_the_row_out_of_the_due_set(store):
    """The whole point: after a failure the row is no longer immediately due."""
    sched = await store.ensure(
        org_id=uuid.uuid4(), agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=1800, config={},
    )
    await _make_due(store)
    claimed = await store.claim_due(worker_id="w1", limit=10)
    assert len(claimed) == 1

    await store.mark_failed(claimed[0])

    # Simulate the lease lapsing -- this is what re-armed the hot loop.
    async with store._sf() as db:
        await db.execute(
            sa.update(AmbientScheduleRow)
            .where(AmbientScheduleRow.id == sched.id)
            .values(locked_until=None)
        )
        await db.commit()

    assert await store.claim_due(worker_id="w1", limit=10) == [], (
        "an expired lease must not resurrect a row whose attempt just failed"
    )


@pytest.mark.asyncio
async def test_mark_failed_clears_the_lock(store):
    """The worker that failed is gone; leaving its name on the row is a lie."""
    await store.ensure(
        org_id=uuid.uuid4(), agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=1800, config={},
    )
    await _make_due(store)
    claimed = await store.claim_due(worker_id="w1", limit=10)
    await store.mark_failed(claimed[0])

    async with store._sf() as db:
        row = (await db.execute(sa.select(AmbientScheduleRow))).scalars().one()
        assert row.locked_by is None
        assert row.locked_until is None


@pytest.mark.asyncio
async def test_mark_failed_backs_off_at_least_one_cadence(store):
    await store.ensure(
        org_id=uuid.uuid4(), agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=1800, config={},
    )
    await _make_due(store)
    claimed = await store.claim_due(worker_id="w1", limit=10)
    before = dt.datetime.now(dt.timezone.utc)
    await store.mark_failed(claimed[0])

    async with store._sf() as db:
        row = (await db.execute(sa.select(AmbientScheduleRow))).scalars().one()
    nxt = row.next_run_at
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=dt.timezone.utc)
    assert (nxt - before).total_seconds() >= 1800


@pytest.mark.asyncio
async def test_a_zero_cadence_schedule_still_backs_off(store):
    """cadence_seconds=0 is used by ensure() to mean "due now".

    Advancing by the cadence alone would leave such a row instantly due
    again -- the hot loop, reintroduced.
    """
    await store.ensure(
        org_id=uuid.uuid4(), agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=0, config={},
    )
    await _make_due(store)
    claimed = await store.claim_due(worker_id="w1", limit=10)
    await store.mark_failed(claimed[0])

    async with store._sf() as db:
        await db.execute(
            sa.update(AmbientScheduleRow).values(locked_until=None)
        )
        await db.commit()

    assert await store.claim_due(worker_id="w1", limit=10) == []
