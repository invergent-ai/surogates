"""Integration tests for scheduled-run materialization.

The platform ticker claims due ``scheduled_sessions`` rows and, for each,
calls :func:`materialize_scheduled_run` to create the actual run session,
emit its prompt, enqueue the *session* id on the shared work queue, and
advance the schedule via ``mark_run_created``.  Stalled dynamic loops are
swept by :func:`recover_stalled_loops`.

These exercise the real DB + Redis (testcontainers).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from surogates.config import SHARED_WORK_QUEUE_KEY, encode_queue_member
from surogates.scheduled.materialize import (
    materialize_scheduled_run,
    recover_stalled_loops,
)
from surogates.scheduled.schedule import (
    parse_dynamic_loop_schedule,
    parse_schedule,
)
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session.events import EventType
from surogates.session.store import SessionStore

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


class FakeStorage:
    def __init__(self) -> None:
        self.buckets: list[str] = []

    async def create_bucket(self, bucket: str) -> None:
        self.buckets.append(bucket)

    def resolve_workspace_path(self, bucket: str, session_id) -> str:
        return f"/workspace/{bucket}/sessions/{session_id}"


class FakeSettings:
    agent_id = "agent-a"
    worker_id = "worker-a"

    class storage:
        bucket = "agent-a-bucket"
        key_prefix = ""

    class llm:
        model = "gpt-4o"

    class scheduled_sessions:
        claim_limit = 10
        claim_lease_seconds = 120


def _deps(session_factory, redis_client, scheduled_store, session_store):
    return dict(
        session_store=session_store,
        scheduled_store=scheduled_store,
        storage=FakeStorage(),
        settings=FakeSettings(),
        redis=redis_client,
    )


async def _claim_one(scheduled_store, worker_id="w1"):
    rows = await scheduled_store.find_due_across_tenants(
        worker_id=worker_id, limit=10,
    )
    return rows


async def _queued(redis_client, *, org_id, agent_id, session_id):
    member = encode_queue_member(
        org_id=str(org_id), agent_id=str(agent_id),
        session_id=str(session_id),
    )
    return await redis_client.zscore(SHARED_WORK_QUEUE_KEY, member)


async def test_materialize_creates_user_session_and_enqueues(
    session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    schedule = await scheduled_store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Health",
        prompt="/status check deployment",
        schedule=parse_schedule("10m"),
        source="loop",
        created_from_session_id=None,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)

    rows = await _claim_one(scheduled_store)
    assert len(rows) == 1
    sid = await materialize_scheduled_run(
        rows[0],
        **_deps(session_factory, redis_client, scheduled_store, session_store),
    )

    updated = await scheduled_store.get(schedule.id)
    assert updated.last_session_id == sid
    assert updated.run_count == 1
    # The advanced cron run is NOT immediately due again.
    assert updated.next_run_at is not None
    assert updated.next_run_at > datetime.now(timezone.utc)

    assert await _queued(
        redis_client, org_id=org_id, agent_id="agent-a", session_id=sid,
    ) is not None

    events = await session_store.get_events(sid)
    assert events[0].type == EventType.USER_MESSAGE.value
    assert events[0].data["content"] == "/status check deployment"


async def test_materialize_marks_dynamic_loop_session(
    session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    schedule = await scheduled_store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        prompt="check CI",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=None,
    )
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)

    rows = await _claim_one(scheduled_store)
    sid = await materialize_scheduled_run(
        rows[0],
        **_deps(session_factory, redis_client, scheduled_store, session_store),
    )

    session = await session_store.get_session(sid)
    assert session.config["scheduled_session_id"] == str(schedule.id)
    assert session.config["scheduled_dynamic_loop"] is True
    # Dynamic loops park (next_run_at None) until loop_wait reschedules.
    updated = await scheduled_store.get(schedule.id)
    assert updated.next_run_at is None


async def test_materialize_links_run_to_origin_session(
    session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    origin = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="agent-a",
        channel="web",
        config={
            "storage_bucket": "agent-a-bucket",
            "storage_key_prefix": "",
            "workspace_path": "/workspace/agent-a-bucket/sessions/origin",
            "supports_vision": False,
        },
    )
    schedule = await scheduled_store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Health",
        prompt="check health",
        schedule=parse_schedule("10m"),
        source="loop",
        created_from_session_id=origin.id,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)

    rows = await _claim_one(scheduled_store)
    sid = await materialize_scheduled_run(
        rows[0],
        **_deps(session_factory, redis_client, scheduled_store, session_store),
    )

    updated = await scheduled_store.get(schedule.id)
    child = await session_store.get_session(updated.last_session_id)
    assert child.id == sid
    assert child.parent_id == origin.id


async def test_materialize_child_inherits_creator_workspace(
    session_factory, redis_client,
):
    from surogates.sandbox.pool import sandbox_session_key

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    creator_workspace = "/workspace/agent-a-bucket/sessions/CREATOR"
    creator = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="agent-a",
        channel="web",
        config={
            "storage_bucket": "agent-a-bucket",
            "storage_key_prefix": "",
            "workspace_path": creator_workspace,
            "supports_vision": False,
        },
    )
    schedule = await scheduled_store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Watcher",
        prompt="status",
        schedule=parse_schedule("10m"),
        source="loop",
        created_from_session_id=creator.id,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)

    rows = await _claim_one(scheduled_store)
    first_sid = await materialize_scheduled_run(
        rows[0],
        **_deps(session_factory, redis_client, scheduled_store, session_store),
    )
    first_child = await session_store.get_session(first_sid)
    assert first_child.parent_id == creator.id
    assert first_child.config["storage_bucket"] == "agent-a-bucket"
    assert first_child.config["workspace_path"] == creator_workspace
    assert first_child.config["sandbox_root_session_id"] == str(creator.id)
    assert sandbox_session_key(first_child) == str(creator.id)

    # Force due again, run a second iteration.
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE scheduled_sessions SET next_run_at = now() - "
                "make_interval(secs => 60) WHERE id = :sid"
            ),
            {"sid": schedule.id},
        )
        await db.commit()

    rows = await _claim_one(scheduled_store)
    second_sid = await materialize_scheduled_run(
        rows[0],
        **_deps(session_factory, redis_client, scheduled_store, session_store),
    )
    second_child = await session_store.get_session(second_sid)
    assert second_child.id != first_child.id
    assert second_child.config["workspace_path"] == creator_workspace
    assert sandbox_session_key(second_child) == sandbox_session_key(first_child)


async def test_materialize_falls_back_to_fresh_workspace_when_creator_missing(
    session_factory, redis_client,
):
    from uuid import uuid4

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)

    missing_creator_id = uuid4()
    schedule = await scheduled_store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Watcher",
        prompt="status",
        schedule=parse_schedule("10m"),
        source="loop",
        created_from_session_id=missing_creator_id,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)

    rows = await _claim_one(scheduled_store)
    sid = await materialize_scheduled_run(
        rows[0],
        **_deps(session_factory, redis_client, scheduled_store, session_store),
    )

    child = await session_store.get_session(sid)
    assert child.config["workspace_path"] == (
        f"/workspace/agent-a-bucket/sessions/{child.id}"
    )
    assert "sandbox_root_session_id" not in child.config
    assert schedule.id is not None


async def test_materialize_creates_sa_owned_run_session(
    session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="loop-runner-sa",
    )
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    schedule = await scheduled_store.create(
        org_id=org_id,
        service_account_id=issued.id,
        agent_id="agent-a",
        name="SA loop",
        prompt="/status",
        schedule=parse_schedule("10m"),
        source="loop",
        created_from_session_id=None,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)

    rows = await _claim_one(scheduled_store)
    sid = await materialize_scheduled_run(
        rows[0],
        **_deps(session_factory, redis_client, scheduled_store, session_store),
    )

    updated = await scheduled_store.get(schedule.id)
    assert updated.last_session_id == sid
    run = await session_store.get_session(sid)
    assert run.user_id is None
    assert run.service_account_id == issued.id


async def test_materialize_idempotent_on_duplicate_fire(
    session_factory, redis_client,
):
    """Two materializations of the same fire (same schedule + next_run_at)
    must converge on one run session via the idempotency key, not create
    a duplicate."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    await scheduled_store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Health",
        prompt="/status",
        schedule=parse_schedule("10m"),
        source="loop",
        created_from_session_id=None,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)

    rows = await _claim_one(scheduled_store)
    row = rows[0]
    sid_a = await materialize_scheduled_run(
        row,
        **_deps(session_factory, redis_client, scheduled_store, session_store),
    )
    sid_b = await materialize_scheduled_run(
        row,
        **_deps(session_factory, redis_client, scheduled_store, session_store),
    )
    assert sid_a == sid_b


async def test_recover_requeues_stalled_dynamic_loop_across_tenants(
    session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    schedule = await scheduled_store.create_dynamic_loop(
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
            "scheduled_session_id": str(schedule.id),
            "scheduled_dynamic_loop": True,
        },
    )
    claimed = (await scheduled_store.claim_due(
        agent_id="agent-a", worker_id="w1", limit=1,
    ))[0]
    await scheduled_store.mark_run_created(claimed, session_id=run_session.id)

    # Make the run look stalled (updated_at far in the past).
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE sessions SET updated_at = now() - "
                "make_interval(secs => 30) WHERE id = :sid"
            ),
            {"sid": run_session.id},
        )
        await db.commit()

    await redis_client.delete(SHARED_WORK_QUEUE_KEY)
    # No agent_id passed → multi-tenant sweep.
    await recover_stalled_loops(
        scheduled_store=scheduled_store,
        redis=redis_client,
        stale_seconds=1,
        limit=100,
    )

    assert await _queued(
        redis_client, org_id=org_id, agent_id="agent-a",
        session_id=run_session.id,
    ) is not None


async def test_mark_run_failed_reschedules_and_releases_claim(
    session_factory, redis_client,
):
    """A run-creation failure must release the lock and reschedule the
    cron schedule so it retries, recording the error."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    await scheduled_store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Health",
        prompt="/status",
        schedule=parse_schedule("10m"),
        source="loop",
        created_from_session_id=None,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    claimed = (await scheduled_store.find_due_across_tenants(
        worker_id="w1", limit=1,
    ))[0]
    assert claimed.locked_by == "w1"

    await scheduled_store.mark_run_failed(claimed, error="kaboom")

    refreshed = await scheduled_store.get(claimed.id)
    assert refreshed.locked_by is None
    assert refreshed.locked_until is None
    assert refreshed.last_error == "kaboom"
    assert refreshed.status == "active"
    assert refreshed.next_run_at is not None
    assert refreshed.next_run_at > datetime.now(timezone.utc)
