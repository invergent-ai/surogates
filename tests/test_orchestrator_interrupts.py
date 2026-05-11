from __future__ import annotations

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
