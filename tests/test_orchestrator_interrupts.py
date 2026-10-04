from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.config import SHARED_WORK_QUEUE_KEY, encode_queue_member
from surogates.orchestrator.dispatcher import Orchestrator


class _BrowserPool:
    def __init__(self) -> None:
        self.destroyed_sessions: list[str] = []

    async def destroy_for_session(self, session_id: str) -> None:
        self.destroyed_sessions.append(session_id)


@pytest.fixture()
def browser_pool() -> _BrowserPool:
    return _BrowserPool()


def _orchestrator(browser_pool: _BrowserPool) -> Orchestrator:
    return Orchestrator(
        redis_client=object(),
        session_store=object(),
        harness_factory=lambda _sid: None,
        agent_id="support-bot",
        queue_key="surogates:work_queue:support-bot",
        max_concurrent=1,
        browser_pool=browser_pool,
    )


async def test_session_deleted_interrupt_destroys_browser_pool(
    browser_pool: _BrowserPool,
) -> None:
    session_id = uuid4()
    orchestrator = _orchestrator(browser_pool)

    await orchestrator._handle_interrupt_signal(session_id, "session deleted")

    assert browser_pool.destroyed_sessions == [str(session_id)]


async def test_pause_interrupt_does_not_destroy_browser_pool(
    browser_pool: _BrowserPool,
) -> None:
    session_id = uuid4()
    orchestrator = _orchestrator(browser_pool)

    await orchestrator._handle_interrupt_signal(session_id, "paused by user")

    assert browser_pool.destroyed_sessions == []


async def test_lease_held_wake_is_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    org_id = uuid4()
    redis = AsyncMock()
    redis.zadd = AsyncMock()

    class LeaseHeldHarness:
        async def wake(self, _session_id):
            return "lease_held"

    session_store = SimpleNamespace(
        get_session=AsyncMock(
            return_value=SimpleNamespace(org_id=org_id, agent_id="support-bot"),
        ),
    )

    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher._LEASE_BUSY_REQUEUE_DELAY",
        0,
        raising=False,
    )
    orchestrator = Orchestrator(
        redis_client=redis,
        session_store=session_store,
        harness_factory=lambda _sid: LeaseHeldHarness(),
        agent_id="support-bot",
        queue_key="surogates:work_queue:support-bot",
        max_concurrent=1,
    )

    await orchestrator._process(session_id)

    redis.zadd.assert_called_once_with(
        SHARED_WORK_QUEUE_KEY,
        {
            encode_queue_member(
                org_id=str(org_id),
                agent_id="support-bot",
                session_id=str(session_id),
            ): 0,
        },
    )


async def test_locally_active_wake_defers_rewake_without_zadd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate _process while the wake is in flight must NOT spin on Redis.

    Regression for the busy-requeue loop where every WORKER_COMPLETE on a
    long coordinator wake produced ~4 enqueue/log cycles per second for
    the wake's full duration.
    """
    session_id = uuid4()
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    active_harness = object()

    def fail_if_called(_session_id):
        raise AssertionError("duplicate local wake should not create a harness")

    orchestrator = Orchestrator(
        redis_client=redis,
        session_store=object(),
        harness_factory=fail_if_called,
        agent_id="support-bot",
        queue_key="surogates:work_queue:support-bot",
        max_concurrent=1,
    )
    orchestrator._active_harnesses[session_id] = active_harness

    await orchestrator._process(session_id)

    assert orchestrator._active_harnesses[session_id] is active_harness
    assert session_id in orchestrator._rewake_pending
    redis.zadd.assert_not_called()


async def test_deferred_rewake_enqueues_once_after_wake_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending rewake flag must produce exactly one enqueue when wake exits."""
    session_id = uuid4()
    org_id = uuid4()
    redis = AsyncMock()
    redis.zadd = AsyncMock()

    class _Harness:
        async def wake(self, _session_id):
            return None

    session_store = SimpleNamespace(
        get_session=AsyncMock(
            return_value=SimpleNamespace(org_id=org_id, agent_id="support-bot"),
        ),
    )

    orchestrator = Orchestrator(
        redis_client=redis,
        session_store=session_store,
        harness_factory=lambda _sid: _Harness(),
        agent_id="support-bot",
        queue_key="surogates:work_queue:support-bot",
        max_concurrent=1,
    )
    # Simulate a parallel _process invocation that flagged a deferred rewake
    # while this wake was already running.
    orchestrator._rewake_pending.add(session_id)

    await orchestrator._process(session_id)

    assert session_id not in orchestrator._rewake_pending
    redis.zadd.assert_called_once_with(
        SHARED_WORK_QUEUE_KEY,
        {
            encode_queue_member(
                org_id=str(org_id),
                agent_id="support-bot",
                session_id=str(session_id),
            ): 0,
        },
    )


async def test_successful_wake_without_pending_flag_does_not_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path must not generate spurious enqueues."""
    session_id = uuid4()
    redis = AsyncMock()
    redis.zadd = AsyncMock()

    class _Harness:
        async def wake(self, _session_id):
            return None

    orchestrator = Orchestrator(
        redis_client=redis,
        session_store=object(),
        harness_factory=lambda _sid: _Harness(),
        agent_id="support-bot",
        queue_key="surogates:work_queue:support-bot",
        max_concurrent=1,
    )

    await orchestrator._process(session_id)

    redis.zadd.assert_not_called()


class _FakePubSub:
    """One pub/sub connection: ``listen`` yields *messages*, then raises *error*."""

    def __init__(self, messages: list[dict], error: Exception | None) -> None:
        self._messages = messages
        self._error = error
        self.closed = False

    async def psubscribe(self, _pattern: str) -> None:
        pass

    async def punsubscribe(self, _pattern: str) -> None:
        raise ConnectionError("connection already dead")

    async def aclose(self) -> None:
        self.closed = True

    async def listen(self):
        for message in self._messages:
            yield message
        if self._error is not None:
            raise self._error
        await asyncio.Event().wait()


async def test_interrupt_listener_resubscribes_after_redis_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped Redis connection must not end the listener for good."""
    session_id = uuid4()
    pubsubs = [
        _FakePubSub([], ConnectionError("Connection reset by peer")),
        _FakePubSub(
            [{
                "type": b"pmessage",
                "channel": f"surogates:interrupt:{session_id}".encode(),
                "data": b'{"reason": "session deleted"}',
            }],
            None,
        ),
    ]
    redis = SimpleNamespace(pubsub=lambda: pubsubs.pop(0))
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher._INTERRUPT_RECONNECT_INITIAL_DELAY", 0,
    )
    orchestrator = Orchestrator(
        redis_client=redis,
        session_store=object(),
        harness_factory=lambda _sid: None,
        agent_id="support-bot",
        queue_key="surogates:work_queue:support-bot",
        max_concurrent=1,
    )
    handled = asyncio.Event()
    received: list[tuple] = []

    async def _handle(sid, reason):
        received.append((sid, reason))
        handled.set()

    orchestrator._handle_interrupt_signal = _handle
    first, second = pubsubs

    task = asyncio.create_task(orchestrator._listen_for_interrupts())
    await asyncio.wait_for(handled.wait(), timeout=2)
    task.cancel()
    await task

    assert received == [(session_id, "session deleted")]
    assert first.closed and second.closed
