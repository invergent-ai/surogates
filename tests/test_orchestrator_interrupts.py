from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

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
    redis = AsyncMock()
    redis.zadd = AsyncMock()

    class LeaseHeldHarness:
        async def wake(self, _session_id):
            return "lease_held"

    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher._LEASE_BUSY_REQUEUE_DELAY",
        0,
        raising=False,
    )
    orchestrator = Orchestrator(
        redis_client=redis,
        session_store=object(),
        harness_factory=lambda _sid: LeaseHeldHarness(),
        agent_id="support-bot",
        queue_key="surogates:work_queue:support-bot",
        max_concurrent=1,
    )

    await orchestrator._process(session_id)

    redis.zadd.assert_called_once_with(
        "surogates:work_queue:support-bot",
        {str(session_id): 0},
    )


async def test_locally_active_wake_is_requeued_without_replacing_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    active_harness = object()

    def fail_if_called(_session_id):
        raise AssertionError("duplicate local wake should not create a harness")

    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher._LEASE_BUSY_REQUEUE_DELAY",
        0,
        raising=False,
    )
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
    redis.zadd.assert_called_once_with(
        "surogates:work_queue:support-bot",
        {str(session_id): 0},
    )
