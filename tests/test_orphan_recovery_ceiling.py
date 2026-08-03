"""The orphan sweeper must stop re-enqueueing a session it cannot recover.

``_sweep_orphans_once`` re-enqueues every stale, leaseless active session it
finds. ``harness.recovered`` is not one of ``find_orphaned_sessions``'s
session-ending event types, so a session that reproducibly kills its worker
before emitting anything stays eligible forever: it takes a worker down on
every sweep, indefinitely.

The dispatcher's crash-loop breaker does not cover this — it only trips inside
the ``except`` branch of ``_process``, and a SIGKILL (OOM, eviction) never
reaches an ``except``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from surogates.orchestrator.dispatcher import (
    _MAX_RECOVERY_ATTEMPTS,
    Orchestrator,
)
from surogates.session.events import EventType

from tests.test_orphan_sweep_gate_release import _FakeQueueRedis


def _make_orphan(*, org_id: UUID, agent_id: str = "agent-X") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), org_id=org_id, agent_id=agent_id, channel="web",
    )


def _make_store(*, orphans: list, streak: int) -> AsyncMock:
    store = AsyncMock()
    store.find_orphaned_sessions = AsyncMock(return_value=orphans)
    store.emit_event = AsyncMock()
    store.release_stale_lease = AsyncMock(return_value=True)
    store.update_session_status = AsyncMock()
    store.count_recoveries_since_progress = AsyncMock(return_value=streak)
    return store


def _make_orchestrator(*, session_store, redis, gate=None) -> Orchestrator:
    return Orchestrator(
        redis_client=redis,
        session_store=session_store,
        harness_factory=lambda _sid: None,
        agent_id="agent-X",
        queue_key="surogates:work_queue",
        max_concurrent=1,
        turn_gate=gate,
    )


def _emitted(store: AsyncMock, event_type: str) -> list:
    return [
        call.args[2]
        for call in store.emit_event.await_args_list
        if call.args[1] == event_type
    ]


@pytest.mark.asyncio
async def test_sweep_stops_re_enqueueing_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
):
    """A session that has burned the recovery budget is not re-enqueued."""
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session", enqueue,
    )

    orphan = _make_orphan(org_id=uuid4())
    store = _make_store(orphans=[orphan], streak=_MAX_RECOVERY_ATTEMPTS)

    orchestrator = _make_orchestrator(
        session_store=store, redis=_FakeQueueRedis(),
    )
    recovered = await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    assert recovered == 0
    assert enqueue.await_count == 0, (
        "a session past the recovery ceiling must not be re-enqueued — "
        "that is the loop this guard exists to break"
    )


@pytest.mark.asyncio
async def test_sweep_fails_the_session_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
):
    """Tripping the ceiling terminates the session so it leaves the
    orphan-eligible set instead of being swept again every cycle."""
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session", AsyncMock(),
    )

    orphan = _make_orphan(org_id=uuid4())
    store = _make_store(orphans=[orphan], streak=_MAX_RECOVERY_ATTEMPTS + 2)

    orchestrator = _make_orchestrator(
        session_store=store, redis=_FakeQueueRedis(),
    )
    await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    fails = _emitted(store, EventType.SESSION_FAIL)
    assert len(fails) == 1, "expected exactly one session.fail"
    assert fails[0]["reason"] == "recovery_loop"
    assert fails[0]["attempts"] == _MAX_RECOVERY_ATTEMPTS + 2
    assert fails[0]["retryable"] is False
    store.update_session_status.assert_awaited_once_with(orphan.id, "failed")


@pytest.mark.asyncio
async def test_ceiling_does_not_emit_harness_recovered(
    monkeypatch: pytest.MonkeyPatch,
):
    """The check runs before the recovery emit.

    ``emit_event`` bumps ``sessions.updated_at``, which is the staleness
    signal — emitting harness.recovered on the tripping pass would reset the
    very clock that made the session visible to the sweeper.
    """
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session", AsyncMock(),
    )

    orphan = _make_orphan(org_id=uuid4())
    store = _make_store(orphans=[orphan], streak=_MAX_RECOVERY_ATTEMPTS)

    orchestrator = _make_orchestrator(
        session_store=store, redis=_FakeQueueRedis(),
    )
    await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    assert _emitted(store, EventType.HARNESS_RECOVERED) == []


@pytest.mark.asyncio
async def test_ceiling_still_releases_the_leaked_gate_slot(
    monkeypatch: pytest.MonkeyPatch,
):
    """The dead owner leaked its gate slot whether or not we retry.

    Skipping the release here would drive the (org, agent) counter to its
    cap and stall every unrelated session for that agent.
    """
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session", AsyncMock(),
    )

    org_id = uuid4()
    orphan = _make_orphan(org_id=org_id)
    store = _make_store(orphans=[orphan], streak=_MAX_RECOVERY_ATTEMPTS)
    gate = AsyncMock()

    orchestrator = _make_orchestrator(
        session_store=store, redis=_FakeQueueRedis(), gate=gate,
    )
    await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    gate.release.assert_awaited_once_with(str(org_id), orphan.agent_id)
    store.release_stale_lease.assert_awaited_once_with(orphan.id)


@pytest.mark.asyncio
async def test_sweep_recovers_normally_below_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
):
    """The guard must not break the recovery it bounds."""
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session", enqueue,
    )

    orphan = _make_orphan(org_id=uuid4())
    store = _make_store(orphans=[orphan], streak=_MAX_RECOVERY_ATTEMPTS - 1)

    orchestrator = _make_orchestrator(
        session_store=store, redis=_FakeQueueRedis(),
    )
    recovered = await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    assert recovered == 1
    assert enqueue.await_count == 1
    assert len(_emitted(store, EventType.HARNESS_RECOVERED)) == 1
    assert _emitted(store, EventType.SESSION_FAIL) == []


@pytest.mark.asyncio
async def test_one_poison_session_does_not_block_its_neighbours(
    monkeypatch: pytest.MonkeyPatch,
):
    """A tripped session must not abort the rest of the sweep pass."""
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session", enqueue,
    )

    org_id = uuid4()
    poison = _make_orphan(org_id=org_id)
    healthy = _make_orphan(org_id=org_id)
    store = _make_store(orphans=[poison, healthy], streak=0)
    store.count_recoveries_since_progress = AsyncMock(
        side_effect=lambda sid: (
            _MAX_RECOVERY_ATTEMPTS if sid == poison.id else 0
        )
    )

    orchestrator = _make_orchestrator(
        session_store=store, redis=_FakeQueueRedis(),
    )
    recovered = await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    assert recovered == 1
    enqueued_ids = {c.kwargs["session_id"] for c in enqueue.await_args_list}
    assert enqueued_ids == {healthy.id}


@pytest.mark.asyncio
async def test_streak_lookup_failure_falls_back_to_recovering(
    monkeypatch: pytest.MonkeyPatch,
):
    """A transient DB blip on the streak query must not strand a session.

    The ceiling is a backstop against a rare poison session; failing open
    keeps ordinary crash recovery — the common case — working.
    """
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session", enqueue,
    )

    orphan = _make_orphan(org_id=uuid4())
    store = _make_store(orphans=[orphan], streak=0)
    store.count_recoveries_since_progress = AsyncMock(
        side_effect=RuntimeError("connection reset")
    )

    orchestrator = _make_orchestrator(
        session_store=store, redis=_FakeQueueRedis(),
    )
    recovered = await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    assert recovered == 1
    assert enqueue.await_count == 1
