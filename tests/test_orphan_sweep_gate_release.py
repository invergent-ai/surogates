"""Boot-time orphan sweeper compensates the TurnConcurrencyGate.

Regression test for a stuck-counter scenario:

  * A worker dies mid-session (debugpy stop, SIGKILL, OOM, pod
    eviction) without running the dispatcher's finally branch.
  * The gate slot for that (org, agent) is leaked -- the counter
    sticks one above where it should be.
  * After a few of these the counter reaches its cap.  Every new
    dequeue then hits ``TurnConcurrencyGate.try_acquire`` over the
    cap, the session is requeued, and the orphan sweeper sees it
    stale 60s later -- producing an endless re-enqueue loop with
    zero diagnostic anywhere in the worker logs.

The fix: when ``_sweep_orphans_once`` recovers a session whose
previous owner died, it also calls ``gate.release(org_id, agent_id)``
to compensate for the leaked acquire.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from surogates.orchestrator.dispatcher import Orchestrator
from surogates.runtime.turn_gate import TurnConcurrencyGate


class _FakeRedisGate:
    """Minimal in-memory shim of the gate's Redis dependency.

    ``TurnConcurrencyGate`` only exercises ``incr`` / ``decr`` so we
    keep it to those.
    """

    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def decr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) - 1
        return self.values[key]


class _FakeQueueRedis:
    """Minimal Redis shim covering the calls ``enqueue_session`` makes
    on the orchestrator's queue object.  We don't assert on what
    landed in the queue here -- a separate test pins that -- but the
    sweeper does call into it so the mock must accept the calls."""

    def __init__(self) -> None:
        self.zadd_calls: list[tuple[str, dict[str, float]]] = []

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zadd_calls.append((key, mapping))
        return len(mapping)

    async def zincrby(self, key: str, amount: float, member: str) -> float:
        return amount


def _make_orphan(*, org_id: UUID, agent_id: str) -> SimpleNamespace:
    """A SimpleNamespace shaped like the Session rows the sweeper iterates."""
    return SimpleNamespace(id=uuid4(), org_id=org_id, agent_id=agent_id)


def _make_orchestrator(
    *,
    session_store: AsyncMock,
    redis: _FakeQueueRedis,
    gate: TurnConcurrencyGate | None,
) -> Orchestrator:
    return Orchestrator(
        redis_client=redis,
        session_store=session_store,
        harness_factory=lambda _sid: None,
        agent_id="agent-X",
        queue_key="surogates:work_queue",
        max_concurrent=1,
        turn_gate=gate,
    )


@pytest.mark.asyncio
async def test_sweep_releases_one_gate_slot_per_recovered_orphan(
    monkeypatch: pytest.MonkeyPatch,
):
    """An orphan recovery must release one gate slot for that (org,
    agent).  Without this, repeated worker crashes drive the counter
    to its cap and every subsequent dequeue is silently rejected."""
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session",
        AsyncMock(),
    )

    org_id = uuid4()
    agent_id = "agent-X"

    gate_redis = _FakeRedisGate()
    gate = TurnConcurrencyGate(gate_redis, default_max=10)

    # Pre-fill the counter to 3 -- representing three previously-
    # leaked slots from worker crashes.
    counter_key = f"surogates:turns:{org_id}:{agent_id}"
    gate_redis.values[counter_key] = 3

    # Two orphans to recover.
    orphans = [
        _make_orphan(org_id=org_id, agent_id=agent_id),
        _make_orphan(org_id=org_id, agent_id=agent_id),
    ]
    session_store = AsyncMock()
    session_store.find_orphaned_sessions = AsyncMock(return_value=orphans)
    session_store.emit_event = AsyncMock()
    session_store.release_stale_lease = AsyncMock(return_value=True)

    orchestrator = _make_orchestrator(
        session_store=session_store,
        redis=_FakeQueueRedis(),
        gate=gate,
    )

    recovered = await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    assert recovered == 2
    # Counter dropped by exactly one per recovered orphan: 3 -> 1.
    assert gate_redis.values[counter_key] == 1, (
        f"expected counter to drop by 2 (one per orphan), got "
        f"{gate_redis.values[counter_key]}"
    )


@pytest.mark.asyncio
async def test_sweep_does_not_drive_gate_below_zero(
    monkeypatch: pytest.MonkeyPatch,
):
    """If the counter is already at 0 when the sweeper runs (e.g. a
    double-recovery from a racy crash), the floor in
    ``TurnGate.release()`` must keep us honest -- the counter cannot
    go negative, because the next genuine acquire would silently
    exceed the cap by the negative offset."""
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session",
        AsyncMock(),
    )

    org_id = uuid4()
    agent_id = "agent-X"

    gate_redis = _FakeRedisGate()
    gate = TurnConcurrencyGate(gate_redis, default_max=10)
    counter_key = f"surogates:turns:{org_id}:{agent_id}"
    gate_redis.values[counter_key] = 0  # already at zero

    orphans = [_make_orphan(org_id=org_id, agent_id=agent_id)]
    session_store = AsyncMock()
    session_store.find_orphaned_sessions = AsyncMock(return_value=orphans)
    session_store.emit_event = AsyncMock()
    session_store.release_stale_lease = AsyncMock(return_value=True)

    orchestrator = _make_orchestrator(
        session_store=session_store,
        redis=_FakeQueueRedis(),
        gate=gate,
    )

    await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    assert gate_redis.values[counter_key] == 0, (
        "counter must not go negative; the floor in release() must "
        f"have re-incremented it (got {gate_redis.values[counter_key]})"
    )


@pytest.mark.asyncio
async def test_sweep_skips_browser_setup_sessions(
    monkeypatch: pytest.MonkeyPatch,
):
    """browser_setup sessions sit ``active`` and leaseless by design (an
    interactive login, no agent loop). The sweeper must skip them — recovering
    one would re-wake it and re-provision the browser out from under the live
    view."""
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session", enqueue
    )

    org_id = uuid4()
    normal = SimpleNamespace(
        id=uuid4(), org_id=org_id, agent_id="agent-X", channel="web"
    )
    setup = SimpleNamespace(
        id=uuid4(), org_id=org_id, agent_id="agent-X", channel="browser_setup"
    )
    session_store = AsyncMock()
    session_store.find_orphaned_sessions = AsyncMock(return_value=[setup, normal])
    session_store.emit_event = AsyncMock()
    session_store.release_stale_lease = AsyncMock(return_value=True)

    orchestrator = _make_orchestrator(
        session_store=session_store, redis=_FakeQueueRedis(), gate=None
    )
    recovered = await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )

    assert recovered == 1  # only the normal session
    assert enqueue.await_count == 1
    emitted = {call.args[0] for call in session_store.emit_event.await_args_list}
    assert normal.id in emitted
    assert setup.id not in emitted


@pytest.mark.asyncio
async def test_sweep_without_gate_still_recovers(
    monkeypatch: pytest.MonkeyPatch,
):
    """The gate is optional; deployments without a TurnConcurrencyGate
    (tests, single-tenant standalone) must still recover orphans."""
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session",
        AsyncMock(),
    )

    orphans = [_make_orphan(org_id=uuid4(), agent_id="agent-X")]
    session_store = AsyncMock()
    session_store.find_orphaned_sessions = AsyncMock(return_value=orphans)
    session_store.emit_event = AsyncMock()
    session_store.release_stale_lease = AsyncMock(return_value=True)

    orchestrator = _make_orchestrator(
        session_store=session_store,
        redis=_FakeQueueRedis(),
        gate=None,
    )

    recovered = await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )
    assert recovered == 1


@pytest.mark.asyncio
async def test_gate_release_failure_does_not_abort_recovery(
    monkeypatch: pytest.MonkeyPatch,
):
    """If gate.release raises (Redis blip, transient network), we must
    still emit harness.recovered, drop the stale lease, and re-enqueue
    -- a flaky gate must not stop the sweeper from making progress."""
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher.enqueue_session",
        AsyncMock(),
    )

    orphans = [_make_orphan(org_id=uuid4(), agent_id="agent-X")]
    session_store = AsyncMock()
    session_store.find_orphaned_sessions = AsyncMock(return_value=orphans)
    session_store.emit_event = AsyncMock()
    session_store.release_stale_lease = AsyncMock(return_value=True)

    class _FlakyGate:
        release = AsyncMock(side_effect=RuntimeError("redis blip"))

    orchestrator = _make_orchestrator(
        session_store=session_store,
        redis=_FakeQueueRedis(),
        gate=_FlakyGate(),  # type: ignore[arg-type]
    )

    recovered = await orchestrator._sweep_orphans_once(
        stale_seconds=60, reason="orchestrator_sweeper",
    )
    assert recovered == 1
    # All three downstream side effects fired despite the gate hiccup.
    session_store.emit_event.assert_awaited_once()
    session_store.release_stale_lease.assert_awaited_once()
