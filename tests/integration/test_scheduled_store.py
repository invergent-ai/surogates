from datetime import datetime, timedelta, timezone

import pytest

from surogates.scheduled.schedule import parse_schedule
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
