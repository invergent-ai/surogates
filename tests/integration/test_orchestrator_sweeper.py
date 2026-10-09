"""Integration test for the orchestrator's orphan sweeper.

Covers the failure mode that the in-process retry path cannot catch:
a worker hard-killed mid-turn.  The sweeper must notice the expired
lease + stale ``updated_at``, emit ``HARNESS_CRASH`` so the event log
explains the gap, drop the stale lease row so ``try_acquire_lease``
doesn't race on the expiry check, and re-enqueue the session so a
live worker can replay it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text

import surogates.harness.loop as loop_module
from surogates.config import SHARED_WORK_QUEUE_KEY, encode_queue_member
from surogates.devices.operations import DeviceOperations
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.session.events import EventType
from tests.test_wake_slash_command_gate import _harness, _permissive

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _backdate(session_factory, session_id, *, seconds: int) -> None:
    """Push a session's updated_at into the past so it trips the stale threshold."""
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE sessions SET updated_at = now() - "
                "make_interval(secs => :s) WHERE id = :sid"
            ),
            {"s": seconds, "sid": session_id},
        )
        await db.commit()


async def test_sweeper_recovers_orphaned_session(
    session_store, session_factory, redis_client, monkeypatch,
):
    """A full sweeper tick recovers an abandoned session end-to-end."""
    # Tight thresholds so the test doesn't have to wait real minutes.
    # The sweeper's initial random offset scales with
    # ``_ORPHAN_SWEEP_INTERVAL``, so we shrink both.
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher._ORPHAN_STALE_SECONDS", 1,
    )
    monkeypatch.setattr(
        "surogates.orchestrator.dispatcher._ORPHAN_SWEEP_INTERVAL", 0.1,
    )

    agent_id = "sweeper-test-agent"
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    # Orphaned: active, no lease, old updated_at.
    orphan = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=agent_id,
    )
    await _backdate(session_factory, orphan.id, seconds=10)

    # Stale lease row left behind by the dead worker.  Force it into the
    # past so try_acquire_lease treats it as expired.
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO session_leases "
                "(session_id, owner_id, lease_token, expires_at, updated_at) "
                "VALUES (:sid, 'dead-worker', gen_random_uuid(), "
                "now() - interval '1 minute', now() - interval '1 minute')"
            ),
            {"sid": orphan.id},
        )
        await db.commit()

    # Healthy session on the same agent — must be ignored.
    healthy = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=agent_id,
    )

    # The work queue is now a single shared sorted-set keyed by an encoded
    # (org, agent, session) member tuple (the per-agent queues were collapsed
    # into SHARED_WORK_QUEUE_KEY). The sweeper re-enqueues via enqueue_session,
    # which writes the encoded member — so membership is checked by member,
    # not by bare session id.
    queue = SHARED_WORK_QUEUE_KEY
    orphan_member = encode_queue_member(
        org_id=str(org_id), agent_id=agent_id, session_id=str(orphan.id),
    )
    healthy_member = encode_queue_member(
        org_id=str(org_id), agent_id=agent_id, session_id=str(healthy.id),
    )
    await redis_client.delete(queue)  # ensure clean state

    # Dummy harness factory — the sweeper never calls it, but Orchestrator
    # requires it for __init__.  A real run loop isn't needed here; we
    # invoke the sweeper body directly via one private call.
    orchestrator = Orchestrator(
        redis_client=redis_client,
        session_store=session_store,
        harness_factory=lambda _sid: None,
        agent_id=agent_id,
        queue_key=queue,
        max_concurrent=1,
    )

    # Drive one sweep iteration by calling the underlying primitives the
    # background task would call -- avoids asyncio.sleep plumbing in the
    # test while still exercising emit/release/enqueue in sequence.
    orphans = await session_store.find_orphaned_sessions(
        stale_seconds=1, agent_id=agent_id,
    )
    assert {o.id for o in orphans} == {orphan.id}, \
        "healthy session should not appear"

    # Invoke the orchestrator's actual recovery path by starting the
    # full sweeper task briefly -- this is what would run inside run().
    orchestrator._running = True
    task = asyncio.create_task(orchestrator._sweep_orphans_forever())
    try:
        # Wait up to 5s for the sweeper to process the orphan.
        for _ in range(50):
            score = await redis_client.zscore(queue, orphan_member)
            if score is not None:
                break
            await asyncio.sleep(0.1)
    finally:
        orchestrator._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Re-enqueued: orphan now sits in the agent's work queue.
    score = await redis_client.zscore(queue, orphan_member)
    assert score is not None, "orphan should be on the agent's work queue"
    healthy_score = await redis_client.zscore(queue, healthy_member)
    assert healthy_score is None, "healthy session must not be enqueued"

    # harness.recovered lands in the event log so audit can explain
    # the gap in the timeline (vs harness.crash which implies an
    # actual exception was raised).
    recovered = await session_store.get_events(
        orphan.id, types=[EventType.HARNESS_RECOVERED],
    )
    assert len(recovered) == 1
    assert recovered[0].data["recovered_by"] == "orchestrator_sweeper"

    # Stale lease was cleared so the next wake's try_acquire_lease
    # doesn't have to depend on the ON-CONFLICT expiry check.
    async with session_factory() as db:
        result = await db.execute(
            text("SELECT count(*) FROM session_leases WHERE session_id = :sid"),
            {"sid": orphan.id},
        )
        assert result.scalar() == 0

    # Clean up.
    await redis_client.delete(queue)


async def test_a_trailing_device_event_leaves_a_finished_turn_alone(session_store, session_factory):
    agent_id = "sweeper-device-agent"
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(user_id=user_id, org_id=org_id, agent_id=agent_id)
    await session_store.emit_event(session.id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "done"}})
    # A cancelled wait's resumed notice can land after the turn's clean end.
    await session_store.emit_event(session.id, EventType.DEVICE_RESUMED, {"device_id": str(uuid4())})
    await _backdate(session_factory, session.id, seconds=10)
    orphans = await session_store.find_orphaned_sessions(stale_seconds=1, agent_id=agent_id)
    assert session.id not in {o.id for o in orphans}


async def test_an_abandoned_session_cancels_what_it_left_on_its_computer(
    session_store, session_factory, redis_client, monkeypatch,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(user_id=user_id, org_id=org_id, agent_id="sweeper-test-agent")
    await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="sweeper-test-agent", parent_id=session.id,
    )
    cancelled: list[set] = []

    async def record(self, calling_session_ids, *, bindings=False):
        cancelled.append(set(calling_session_ids))
        return 0

    monkeypatch.setattr(DeviceOperations, "cancel", record)
    orchestrator = Orchestrator(
        redis_client=redis_client,
        session_store=session_store,
        harness_factory=lambda _sid: None,
        agent_id="sweeper-test-agent",
        queue_key="surogates:work_queue:sweeper-test-agent",
        session_factory=session_factory,
    )
    await orchestrator._abandon_unrecoverable_session(session, attempts=3, reason="no progress")
    assert cancelled == [{session.id}]


def _sweeper(session_store, redis_client, agent_id: str) -> Orchestrator:
    return Orchestrator(
        redis_client=redis_client,
        session_store=session_store,
        harness_factory=lambda _sid: None,
        agent_id=agent_id,
        queue_key=SHARED_WORK_QUEUE_KEY,
    )


async def _stuck(session_store, session_factory, agent_id: str, *events) -> object:
    """An active, leaseless session of *agent_id* whose last events are *events*."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(user_id=user_id, org_id=org_id, agent_id=agent_id)
    await session_store.emit_event(session.id, EventType.USER_MESSAGE, {"content": "Check the figures."})
    for event_type, data in events:
        await session_store.emit_event(session.id, event_type, data)
    return session


@pytest.mark.parametrize("age, recovered", [(120, 1), (7200, 0)], ids=["2-minutes", "2-hours"])
async def test_a_session_whose_worker_died_after_a_recent_crash_is_recovered(
    session_store, session_factory, redis_client, age, recovered,
):
    # The dispatcher retries a crash a second or two later. A worker killed in
    # between leaves the crash as the last event, and nothing else to wake it.
    # A crash older than an hour is left alone: its turn is long past.
    agent_id = f"sweeper-crash-agent-{age}"
    session = await _stuck(session_store, session_factory, agent_id, (EventType.HARNESS_CRASH, {"error": "the hub timed out"}))
    async with session_factory() as db:
        await db.execute(
            text("UPDATE events SET created_at = now() - make_interval(secs => :s) WHERE session_id = :sid"),
            {"s": age, "sid": session.id},
        )
        await db.commit()
    await _backdate(session_factory, session.id, seconds=age)
    sweeper = _sweeper(session_store, redis_client, agent_id)
    assert await sweeper._sweep_orphans_once(stale_seconds=60, reason="orchestrator_sweeper") == recovered
    member = encode_queue_member(org_id=str(session.org_id), agent_id=agent_id, session_id=str(session.id))
    assert await redis_client.zrem(SHARED_WORK_QUEUE_KEY, member) == recovered


async def test_a_response_with_null_tool_calls_does_not_stop_the_sweep(session_store, session_factory, redis_client):
    agent_id = "sweeper-null-tools-agent"
    answered = await _stuck(session_store, session_factory, agent_id, (
        EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "Done.", "tool_calls": None}},
    ))
    orphan = await _stuck(session_store, session_factory, agent_id)
    for session in (answered, orphan):
        await _backdate(session_factory, session.id, seconds=120)
    sweeper = _sweeper(session_store, redis_client, agent_id)
    assert await sweeper._sweep_orphans_once(stale_seconds=60, reason="orchestrator_sweeper") == 1
    member = encode_queue_member(org_id=str(orphan.org_id), agent_id=agent_id, session_id=str(orphan.id))
    assert await redis_client.zrem(SHARED_WORK_QUEUE_KEY, member) == 1


async def _read_through(session_store, session, event_id: int) -> None:
    """*session*'s worker read its log through *event_id*: its cursor is there."""
    lease = await session_store.try_acquire_lease(session.id, "the-turns-worker")
    await session_store.advance_harness_cursor(session.id, event_id, lease.lease_token)
    await session_store.release_lease(session.id, lease.lease_token)


async def _turns_of_a_wake(session_store, monkeypatch, session) -> int:
    """Wake *session* as a worker does, with the real store: how many turns the wake ran."""
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    harness = _harness(session_store, _permissive())
    # The helper's compressor is a spec mock: left alone it hands the turn a mock instead of the messages.
    harness._compressor.prune_stale_browser_states = lambda messages: messages
    harness._run_loop = AsyncMock()
    await asyncio.wait_for(harness.wake(session.id), 10.0)
    return harness._run_loop.await_count


@pytest.mark.parametrize("opened", ["before", "after"])
async def test_a_worker_that_died_with_one_of_two_calls_answered_is_recovered_whichever_side_its_browser_opened(
    session_store, session_factory, redis_client, monkeypatch, opened,
):
    # The model asked for two tools at once. One answered, which moved the cursor; the other opened the
    # chat's browser and was still running when the worker died.
    agent_id = f"sweeper-sibling-agent-{opened}"
    calls = [
        {"id": "a", "type": "function", "function": {"name": "browser_navigate", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
    ]
    session = await _stuck(
        session_store, session_factory, agent_id,
        (EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "", "tool_calls": calls}}),
        (EventType.TOOL_CALL, {"tool_call_id": "a", "name": "browser_navigate"}),
        (EventType.TOOL_CALL, {"tool_call_id": "b", "name": "read_file"}),
    )
    answered = (EventType.TOOL_RESULT, {"tool_call_id": "b", "name": "read_file", "content": "Q3 plan"})
    browser = (EventType.BROWSER_PROVISIONED, {"session_id": str(session.id), "browser_id": "b-1"})
    for kind, data in ([browser, answered] if opened == "before" else [answered, browser]):
        event_id = await session_store.emit_event(session.id, kind, data)
        if kind is EventType.TOOL_RESULT:
            await _read_through(session_store, session, event_id)
    await _backdate(session_factory, session.id, seconds=120)
    member = encode_queue_member(org_id=str(session.org_id), agent_id=agent_id, session_id=str(session.id))

    try:
        assert [o.id for o in await session_store.find_orphaned_sessions(stale_seconds=60, agent_id=agent_id)] == [session.id]
        sweeper = _sweeper(session_store, redis_client, agent_id)
        assert await sweeper._sweep_orphans_once(stale_seconds=60, reason="orchestrator_sweeper") == 1
        assert await redis_client.zscore(SHARED_WORK_QUEUE_KEY, member) is not None
        # The wake the sweeper queued finds the call left unanswered, and runs the turn.
        assert await _turns_of_a_wake(session_store, monkeypatch, session) == 1
    finally:
        await redis_client.zrem(SHARED_WORK_QUEUE_KEY, member)


@pytest.mark.parametrize(
    "since", [EventType.BROWSER_PROVISIONED, EventType.BROWSER_DESTROYED], ids=lambda kind: kind.value,
)
async def test_an_idle_chat_whose_browser_opened_or_closed_since_its_turn_is_left_alone(
    session_store, session_factory, redis_client, monkeypatch, since,
):
    # Its turn ended with an answer, every call answered: the browser's own events after it are no turn's.
    agent_id = f"sweeper-idle-browser-agent-{since.value}"
    call = {"id": "a", "type": "function", "function": {"name": "browser_navigate", "arguments": "{}"}}
    session = await _stuck(
        session_store, session_factory, agent_id,
        (EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "", "tool_calls": [call]}}),
        (EventType.TOOL_CALL, {"tool_call_id": "a", "name": "browser_navigate"}),
        (EventType.TOOL_RESULT, {"tool_call_id": "a", "name": "browser_navigate", "content": "{}"}),
    )
    answer = await session_store.emit_event(
        session.id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "It is open."}},
    )
    await _read_through(session_store, session, answer)
    await session_store.emit_event(session.id, since, {"session_id": str(session.id), "browser_id": "b-1"})
    await _backdate(session_factory, session.id, seconds=120)

    assert await session_store.find_orphaned_sessions(stale_seconds=60, agent_id=agent_id) == []
    sweeper = _sweeper(session_store, redis_client, agent_id)
    assert await sweeper._sweep_orphans_once(stale_seconds=60, reason="orchestrator_sweeper") == 0
    # And a wake that came all the same would run nothing, and write nothing.
    written = len(await session_store.get_events(session.id))
    assert await _turns_of_a_wake(session_store, monkeypatch, session) == 0
    assert len(await session_store.get_events(session.id)) == written
