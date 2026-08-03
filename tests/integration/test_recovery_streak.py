"""``count_recoveries_since_progress`` — the orphan sweeper's poison guard.

The sweeper re-enqueues any stale, leaseless active session. ``harness.recovered``
is not a session-ending event, so a session that reproducibly kills its worker
before emitting anything stays orphan-eligible forever and takes a worker down
every sweep. The crash-loop breaker cannot see it: that only trips inside an
``except`` block, and a SIGKILL never reaches one.

The streak counts recoveries since the session last showed a sign of life, so
a session doing real work between crashes is never mistaken for a poison one.
"""

from __future__ import annotations

import uuid

import pytest

from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_session(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    return await session_store.create_session(
        org_id=org_id, user_id=user_id, agent_id="agent-1", channel="web",
    )


async def test_streak_is_zero_for_a_session_that_never_crashed(
    session_store, session_factory,
):
    session = await _make_session(session_store, session_factory)
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "hello"},
    )

    assert await session_store.count_recoveries_since_progress(session.id) == 0


async def test_streak_counts_consecutive_recoveries(
    session_store, session_factory,
):
    """Three sweeps that never produced a sign of life count as three."""
    session = await _make_session(session_store, session_factory)
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "do it"},
    )
    for _ in range(3):
        await session_store.emit_event(
            session.id, EventType.HARNESS_RECOVERED, {"recovered_by": "sweeper"},
        )

    assert await session_store.count_recoveries_since_progress(session.id) == 3


@pytest.mark.parametrize(
    "progress_type",
    [EventType.LLM_RESPONSE, EventType.TOOL_RESULT, EventType.USER_MESSAGE],
)
async def test_progress_after_a_recovery_resets_the_streak(
    session_store, session_factory, progress_type,
):
    """A session that gets real work done between crashes is not poison."""
    session = await _make_session(session_store, session_factory)
    for _ in range(5):
        await session_store.emit_event(
            session.id, EventType.HARNESS_RECOVERED, {"recovered_by": "sweeper"},
        )
    await session_store.emit_event(session.id, progress_type, {})
    await session_store.emit_event(
        session.id, EventType.HARNESS_RECOVERED, {"recovered_by": "sweeper"},
    )

    assert await session_store.count_recoveries_since_progress(session.id) == 1


async def test_streak_ignores_events_that_are_not_progress(
    session_store, session_factory,
):
    """An ``llm.request`` means the wake started, not that it accomplished
    anything — a session that OOMs after every request is still poison."""
    session = await _make_session(session_store, session_factory)
    for _ in range(3):
        await session_store.emit_event(
            session.id, EventType.HARNESS_RECOVERED, {"recovered_by": "sweeper"},
        )
        await session_store.emit_event(session.id, EventType.LLM_REQUEST, {})

    assert await session_store.count_recoveries_since_progress(session.id) == 3


async def test_streak_is_scoped_to_one_session(session_store, session_factory):
    session_a = await _make_session(session_store, session_factory)
    session_b = await _make_session(session_store, session_factory)
    for _ in range(4):
        await session_store.emit_event(
            session_a.id, EventType.HARNESS_RECOVERED, {"recovered_by": "sweeper"},
        )

    assert await session_store.count_recoveries_since_progress(session_b.id) == 0


async def test_streak_of_a_session_with_no_events_is_zero(
    session_store, session_factory,
):
    session = await _make_session(session_store, session_factory)

    assert await session_store.count_recoveries_since_progress(session.id) == 0


async def test_unknown_session_has_no_streak(session_store):
    assert await session_store.count_recoveries_since_progress(uuid.uuid4()) == 0
