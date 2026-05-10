from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from surogates.scheduled.schedule import (
    DYNAMIC_LOOP_EXPIRY_DAYS,
    DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS,
    parse_dynamic_loop_schedule,
    parse_schedule,
)
from surogates.scheduled.store import ScheduledSessionStore

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_create_and_list_user_owned_schedule(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    created = await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Deploy check",
        prompt="Check deploy health",
        schedule=parse_schedule("10m"),
        source="tool",
        created_from_session_id=None,
    )

    rows = await store.list_for_user(org_id=org_id, user_id=user_id, agent_id="agent-a")
    assert [row.id for row in rows] == [created.id]
    assert rows[0].status == "active"
    assert rows[0].next_run_at is not None


async def test_claim_due_is_agent_scoped_and_skip_locked_safe(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    due = datetime.now(timezone.utc) - timedelta(minutes=1)

    a = await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="A",
        prompt="Run A",
        schedule=parse_schedule("10m"),
        source="tool",
        created_from_session_id=None,
        next_run_at=due,
    )
    await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-b",
        name="B",
        prompt="Run B",
        schedule=parse_schedule("10m"),
        source="tool",
        created_from_session_id=None,
        next_run_at=due,
    )

    first = await store.claim_due(agent_id="agent-a", worker_id="w1", limit=10)
    second = await store.claim_due(agent_id="agent-a", worker_id="w2", limit=10)

    assert [row.id for row in first] == [a.id]
    assert second == []


async def test_mark_run_created_advances_or_expires(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    created = await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Once",
        prompt="Run once",
        schedule=parse_schedule("10m"),
        source="tool",
        created_from_session_id=None,
        repeat_limit=1,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=created.id)
    updated = await store.get(created.id)
    assert updated.status == "completed"
    assert updated.run_count == 1


async def test_dynamic_loop_lifecycle_waits_for_loop_wait(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    created = await store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        prompt="check CI",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=None,
    )

    assert created.schedule["kind"] == "dynamic_loop"
    assert created.next_run_at is not None
    assert created.expires_at is not None
    ttl = created.expires_at - created.created_at
    assert timedelta(days=DYNAMIC_LOOP_EXPIRY_DAYS) - timedelta(seconds=1) <= ttl
    assert ttl <= timedelta(days=DYNAMIC_LOOP_EXPIRY_DAYS, seconds=1)

    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=created.id)

    waiting = await store.get(created.id)
    assert waiting.status == "active"
    assert waiting.run_count == 1
    assert waiting.next_run_at is None

    await store.mark_dynamic_run_finished(
        schedule_id=created.id,
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        session_id=created.id,
        delay_seconds=120,
        reason="CI is still running",
    )

    updated = await store.get(created.id)
    assert updated.next_run_at is not None
    assert updated.schedule["last_delay_seconds"] == 120
    assert updated.schedule["last_delay_reason"] == "CI is still running"


async def test_terminal_dynamic_loop_without_wait_gets_fallback(
    session_factory,
    session_store,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    loop = await store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        prompt="check CI",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=None,
    )
    run_session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="agent-a",
        channel="scheduled",
        config={
            "scheduled_session_id": str(loop.id),
            "scheduled_dynamic_loop": True,
        },
    )

    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=run_session.id)
    await session_store.update_session_status(run_session.id, "completed")

    recovered = await store.recover_stalled_dynamic_loops(
        agent_id="agent-a",
        stale_seconds=1,
    )

    assert [row.id for row in recovered] == [loop.id]
    updated = await store.get(loop.id)
    assert updated.next_run_at is not None
    assert updated.schedule["last_delay_seconds"] == DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS
    assert "fallback" in updated.schedule["last_delay_reason"]


async def test_find_retryable_stalled_dynamic_loop_runs(
    session_factory,
    session_store,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    loop = await store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        prompt="check CI",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=None,
    )
    run_session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="agent-a",
        channel="scheduled",
        config={
            "scheduled_session_id": str(loop.id),
            "scheduled_dynamic_loop": True,
        },
    )

    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=run_session.id)

    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE sessions SET updated_at = now() - make_interval(secs => 10) "
                "WHERE id = :sid"
            ),
            {"sid": run_session.id},
        )
        await db.commit()

    retryable = await store.find_retryable_stalled_dynamic_loop_runs(
        agent_id="agent-a",
        stale_seconds=1,
    )

    assert [(row.id, row.last_session_id) for row in retryable] == [
        (loop.id, run_session.id),
    ]
    updated = await store.get(loop.id)
    assert updated.next_run_at is None
