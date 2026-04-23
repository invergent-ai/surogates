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

import pytest
from sqlalchemy import text

from surogates.config import agent_queue_key
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.session.events import EventType

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

    queue = agent_queue_key(agent_id)
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
            score = await redis_client.zscore(queue, str(orphan.id))
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
    score = await redis_client.zscore(queue, str(orphan.id))
    assert score is not None, "orphan should be on the agent's work queue"
    healthy_score = await redis_client.zscore(queue, str(healthy.id))
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
