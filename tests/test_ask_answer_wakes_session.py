"""An answer must reach a session nobody is parked on.

`ask_user_question` parks a poller for up to 30 minutes. While that poller
lives it sees the answer within a second and nothing else is needed. But the
poller is not guaranteed to be there: the worker may have died, or the wait
may already have timed out. In that case the answer landed in the event log
with no one listening and the session sat idle holding it.

Waking is conditional on there being no live lease. Enqueueing while a poller
holds the session would ride the dispatcher's `_rewake_pending` path and run
an extra turn on unchanged history once the current one finished.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.session.interactive_input import wake_if_unattended


@pytest.mark.asyncio
async def test_an_unattended_session_is_woken():
    store = AsyncMock()
    store.has_live_lease = AsyncMock(return_value=False)
    store.get_session = AsyncMock(
        return_value=type("S", (), {"org_id": uuid4(), "agent_id": "a"})(),
    )
    enqueue = AsyncMock()

    await wake_if_unattended(
        store, redis=object(), session_id=uuid4(), _enqueue=enqueue,
    )

    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_session_with_a_live_poller_is_left_alone():
    """The parked poller will see the answer itself within a second."""
    store = AsyncMock()
    store.has_live_lease = AsyncMock(return_value=True)
    enqueue = AsyncMock()

    await wake_if_unattended(
        store, redis=object(), session_id=uuid4(), _enqueue=enqueue,
    )

    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_redis_is_a_noop():
    store = AsyncMock()
    store.has_live_lease = AsyncMock(return_value=False)
    enqueue = AsyncMock()

    await wake_if_unattended(
        store, redis=None, session_id=uuid4(), _enqueue=enqueue,
    )

    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_wake_failure_does_not_propagate():
    """The answer is already recorded; failing the request would tell the
    user their answer was lost when it was not."""
    store = AsyncMock()
    store.has_live_lease = AsyncMock(return_value=False)
    store.get_session = AsyncMock(side_effect=RuntimeError("gone"))

    await wake_if_unattended(
        store, redis=object(), session_id=uuid4(), _enqueue=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_a_lease_check_failure_does_not_wake():
    """Unknown attendance is treated as attended: a spurious extra turn is
    worse than a slightly late answer, which the poller still picks up."""
    store = AsyncMock()
    store.has_live_lease = AsyncMock(side_effect=RuntimeError("db blip"))
    enqueue = AsyncMock()

    await wake_if_unattended(
        store, redis=object(), session_id=uuid4(), _enqueue=enqueue,
    )

    enqueue.assert_not_awaited()
