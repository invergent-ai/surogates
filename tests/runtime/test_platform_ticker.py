"""Tests for PlatformTicker.

Acquires the leader lock, polls multi-tenant due rows, materializes a
run session per row (``run_one``), and periodically recovers stalled
dynamic loops (``recover``).  Sleeps, loops.  Loss-of-lock or shutdown
signal exits cleanly.
"""

from __future__ import annotations

import asyncio

import pytest


class _StubLock:
    def __init__(
        self, *,
        acquire_returns: bool = True,
        heartbeat_returns: bool = True,
    ) -> None:
        self._acquire = acquire_returns
        self._heartbeat = heartbeat_returns
        self.released = False
        self.acquire_calls = 0
        self.heartbeat_calls = 0

    async def acquire(self) -> bool:
        self.acquire_calls += 1
        return self._acquire

    async def heartbeat(self) -> bool:
        self.heartbeat_calls += 1
        return self._heartbeat

    async def release(self) -> None:
        self.released = True


class _StubStore:
    def __init__(self, due_rows: list[dict]) -> None:
        self.due_rows = due_rows
        self.calls = 0
        self.failed: list[tuple] = []

    async def find_due_across_tenants(self, **_):
        self.calls += 1
        if self.calls == 1:
            return list(self.due_rows)
        return []

    async def mark_run_failed(self, schedule, *, error):
        self.failed.append((schedule, error))


@pytest.mark.asyncio
async def test_ticker_materializes_due_rows_when_leader():
    """The leader claims due rows and runs ``run_one`` for each — the
    materialization seam that creates the run session.  The old behaviour
    (enqueueing the schedule row id directly onto the work queue) was the
    bug: no session ever existed for that id, so the dispatcher failed
    every tick with SessionNotFoundError."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock(acquire_returns=True, heartbeat_returns=True)
    store = _StubStore(due_rows=[
        {"agent_id": "a-1", "org_id": "o-1", "id": "s-1"},
        {"agent_id": "a-2", "org_id": "o-2", "id": "s-2"},
    ])
    materialized: list = []

    async def run_one(row):
        materialized.append(row)

    ticker = PlatformTicker(
        lock=lock, store=store, run_one=run_one,
        tick_interval_seconds=0.01, worker_id="ticker-a",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    assert [r["id"] for r in materialized] == ["s-1", "s-2"]
    assert lock.released is True


@pytest.mark.asyncio
async def test_ticker_marks_run_failed_when_materialization_raises():
    """A run that fails to materialize must be recorded via
    ``mark_run_failed`` (which releases the claim + reschedules) so the
    schedule retries instead of staying locked or silently lost.  One
    bad row must not abort the rest of the tick."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock()
    store = _StubStore(due_rows=[
        {"agent_id": "a-1", "org_id": "o-1", "id": "boom"},
        {"agent_id": "a-2", "org_id": "o-2", "id": "ok"},
    ])
    materialized: list = []

    async def run_one(row):
        if row["id"] == "boom":
            raise RuntimeError("kaboom")
        materialized.append(row)

    ticker = PlatformTicker(
        lock=lock, store=store, run_one=run_one,
        tick_interval_seconds=0.01, worker_id="ticker-f",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    # The failing row was marked failed...
    assert len(store.failed) == 1
    failed_row, error = store.failed[0]
    assert failed_row["id"] == "boom"
    assert "kaboom" in error
    # ...and the healthy row still ran.
    assert [r["id"] for r in materialized] == ["ok"]


@pytest.mark.asyncio
async def test_ticker_runs_recovery_each_tick():
    """When a ``recover`` callable is supplied it runs once per tick
    (under the lock) to requeue/recover stalled dynamic loops."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock()
    store = _StubStore(due_rows=[])
    recover_calls = 0

    async def run_one(row):  # pragma: no cover - no due rows here
        pass

    async def recover():
        nonlocal recover_calls
        recover_calls += 1

    ticker = PlatformTicker(
        lock=lock, store=store, run_one=run_one, recover=recover,
        tick_interval_seconds=0.01, worker_id="ticker-g",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    assert recover_calls >= 1


@pytest.mark.asyncio
async def test_ticker_recovery_failure_does_not_abort_tick():
    """A throwing ``recover`` must not stop due rows from being
    materialized — recovery is best-effort."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock()
    store = _StubStore(due_rows=[{"agent_id": "a-1", "org_id": "o-1", "id": "s-1"}])
    materialized: list = []

    async def run_one(row):
        materialized.append(row)

    async def recover():
        raise RuntimeError("recovery blew up")

    ticker = PlatformTicker(
        lock=lock, store=store, run_one=run_one, recover=recover,
        tick_interval_seconds=0.01, worker_id="ticker-h",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    assert [r["id"] for r in materialized] == ["s-1"]


@pytest.mark.asyncio
async def test_ticker_does_not_dispatch_when_not_leader():
    """When acquire returns False (another replica is leader),
    the ticker sleeps without dispatching.  The DB query is
    NOT issued -- saves a round-trip when we're not the
    leader."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock(acquire_returns=False)
    store = _StubStore(due_rows=[
        {"agent_id": "a-1", "org_id": "o-1", "id": "s-1"},
    ])
    materialized: list = []

    async def run_one(row):
        materialized.append(row)

    ticker = PlatformTicker(
        lock=lock, store=store, run_one=run_one,
        tick_interval_seconds=0.01, worker_id="ticker-b",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    assert materialized == []
    assert store.calls == 0


@pytest.mark.asyncio
async def test_ticker_stops_dispatching_on_loss_of_lock():
    """Loss-of-lock mid-tick: heartbeat returns False; the
    ticker MUST stop dispatching this tick to avoid double-fire
    when the new leader picks up the same rows on its own
    tick."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    class _LosingLock:
        def __init__(self):
            self.released = False

        async def acquire(self):
            return True

        async def heartbeat(self):
            return False  # always lost

        async def release(self):
            self.released = True

    lock = _LosingLock()
    store = _StubStore(due_rows=[
        {"agent_id": "a-1", "org_id": "o-1", "id": "s-1"},
    ])
    materialized: list = []

    async def run_one(row):
        materialized.append(row)

    ticker = PlatformTicker(
        lock=lock, store=store, run_one=run_one,
        tick_interval_seconds=0.01, worker_id="ticker-c",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    assert materialized == []
    assert lock.released is True


@pytest.mark.asyncio
async def test_ticker_releases_lock_on_cancellation():
    """request_stop() must release the lock cleanly so a fresh
    contender on a different replica can acquire on the next
    tick."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock()
    store = _StubStore(due_rows=[])

    async def run_one(row):  # pragma: no cover - no due rows
        pass

    ticker = PlatformTicker(
        lock=lock, store=store, run_one=run_one,
        tick_interval_seconds=0.5, worker_id="ticker-d",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    assert lock.released is True


@pytest.mark.asyncio
async def test_ticker_heartbeats_twice_per_tick():
    """The two heartbeat calls (one before the DB read, one
    before materialization) catch a slow DB read pushing us past
    the TTL boundary -- if the DB took too long, the second
    heartbeat returns False and we drop the rows for this
    tick rather than double-fire."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock()
    store = _StubStore(due_rows=[
        {"agent_id": "a-1", "org_id": "o-1", "id": "s-1"},
    ])

    async def run_one(row):
        pass

    ticker = PlatformTicker(
        lock=lock, store=store, run_one=run_one,
        tick_interval_seconds=0.01, worker_id="ticker-e",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    # At least 2 heartbeats fired (one before DB read, one
    # before materialization) per successful tick.
    assert lock.heartbeat_calls >= 2
