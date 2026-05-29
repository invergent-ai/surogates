"""Tests for PlatformTicker.

Plan 8 / Task 5.  Acquires the leader lock, polls multi-tenant
due rows, enqueues, sleeps, loops.  Loss-of-lock or shutdown
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

    async def find_due_across_tenants(self, **_):
        self.calls += 1
        if self.calls == 1:
            return list(self.due_rows)
        return []


@pytest.mark.asyncio
async def test_ticker_enqueues_due_rows_when_leader():
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock(acquire_returns=True, heartbeat_returns=True)
    store = _StubStore(due_rows=[
        {"agent_id": "a-1", "org_id": "o-1", "id": "s-1"},
        {"agent_id": "a-2", "org_id": "o-2", "id": "s-2"},
    ])
    enqueued: list[dict] = []

    async def enqueue(*, org_id, agent_id, session_id, priority=0):
        enqueued.append({
            "org_id": org_id, "agent_id": agent_id,
            "session_id": session_id,
        })

    ticker = PlatformTicker(
        lock=lock, store=store, enqueue=enqueue,
        tick_interval_seconds=0.01, worker_id="ticker-a",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    assert len(enqueued) == 2
    assert lock.released is True


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
    enqueued: list = []

    async def enqueue(**_):
        enqueued.append({})

    ticker = PlatformTicker(
        lock=lock, store=store, enqueue=enqueue,
        tick_interval_seconds=0.01, worker_id="ticker-b",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    assert enqueued == []
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
    enqueued: list = []

    async def enqueue(**_):
        enqueued.append({})

    ticker = PlatformTicker(
        lock=lock, store=store, enqueue=enqueue,
        tick_interval_seconds=0.01, worker_id="ticker-c",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    assert enqueued == []
    assert lock.released is True


@pytest.mark.asyncio
async def test_ticker_releases_lock_on_cancellation():
    """request_stop() must release the lock cleanly so a fresh
    contender on a different replica can acquire on the next
    tick."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock()
    store = _StubStore(due_rows=[])

    async def enqueue(**_):
        pass

    ticker = PlatformTicker(
        lock=lock, store=store, enqueue=enqueue,
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
    before enqueue) catch a slow DB read pushing us past the
    TTL boundary -- if the DB took too long, the second
    heartbeat returns False and we drop the rows for this
    tick rather than double-fire."""
    from surogates.scheduled.platform_ticker import PlatformTicker

    lock = _StubLock()
    store = _StubStore(due_rows=[
        {"agent_id": "a-1", "org_id": "o-1", "id": "s-1"},
    ])

    async def enqueue(**_):
        pass

    ticker = PlatformTicker(
        lock=lock, store=store, enqueue=enqueue,
        tick_interval_seconds=0.01, worker_id="ticker-e",
    )
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)
    ticker.request_stop()
    await task

    # At least 2 heartbeats fired (one before DB read, one
    # before enqueue) per successful tick.
    assert lock.heartbeat_calls >= 2
