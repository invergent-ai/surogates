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
    schedule = await scheduled_store.create_loop(
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

    # The first run ends; force due again, run a second iteration.
    await session_store.update_session_status(first_sid, "completed")
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


async def test_a_run_logs_in_as_its_schedule_captured_not_as_its_chat_now(
    session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    creator = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-a", channel="studio",
        config={
            "storage_bucket": "agent-a-bucket", "storage_key_prefix": "",
            "workspace_path": "/workspace/agent-a-bucket/sessions/C",
            "browser": {"profile_id": "bound-later"},
        },
    )
    attached = await scheduled_store.create(
        org_id=org_id, user_id=user_id, agent_id="agent-a", name="Post",
        prompt="post", schedule=parse_schedule("10m"), source="loop",
        created_from_session_id=creator.id,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    detached = await scheduled_store.create(
        org_id=org_id, user_id=user_id, agent_id="agent-a", name="Post 2",
        prompt="post", schedule=parse_schedule("10m"), source="loop",
        created_from_session_id=None,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE scheduled_sessions SET context = CAST(:ctx AS jsonb) "
                "WHERE id IN (:a, :d)"
            ),
            {
                "ctx": '{"browser": {"profile_id": "captured"}, "origin_channel": "studio"}',
                "a": attached.id, "d": detached.id,
            },
        )
        await db.commit()
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)

    for row in await _claim_one(scheduled_store):
        sid = await materialize_scheduled_run(
            row, **_deps(session_factory, redis_client, scheduled_store, session_store),
        )
        run = await session_store.get_session(sid)
        assert run.config["browser"] == {"profile_id": "captured"}
        assert run.config["origin_channel"] == "studio"


async def test_a_run_of_a_schedule_without_a_captured_context_takes_its_chats(
    session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    creator = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-a", channel="studio",
        config={
            "storage_bucket": "agent-a-bucket", "storage_key_prefix": "",
            "workspace_path": "/workspace/agent-a-bucket/sessions/C",
            "browser": {"profile_id": "chat-profile"},
        },
    )
    await scheduled_store.create(
        org_id=org_id, user_id=user_id, agent_id="agent-a", name="Legacy",
        prompt="post", schedule=parse_schedule("10m"), source="loop",
        created_from_session_id=creator.id,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)

    [row] = await _claim_one(scheduled_store)
    sid = await materialize_scheduled_run(
        row, **_deps(session_factory, redis_client, scheduled_store, session_store),
    )
    run = await session_store.get_session(sid)
    assert run.config["browser"] == {"profile_id": "chat-profile"}


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
    schedule = await scheduled_store.create_loop(
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


async def test_recover_stalled_loops_reaps_expired_active_schedule(
    session_factory, redis_client
):
    """The recovery sweep also transitions schedules past their expires_at
    from 'active' to 'completed' — a cron loop that expired between ticks can
    never be re-claimed, so this is the only thing that reaps it.
    """
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    now = datetime.now(timezone.utc)

    zombie = await scheduled_store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Expired loop",
        prompt="tick",
        schedule=parse_schedule("1m"),
        source="loop",
        created_from_session_id=None,
        next_run_at=now - timedelta(minutes=1),
        expires_at=now - timedelta(hours=1),
    )

    await recover_stalled_loops(
        scheduled_store=scheduled_store,
        redis=redis_client,
        stale_seconds=1,
        limit=100,
    )

    reaped = await scheduled_store.get(zombie.id)
    assert reaped.status == "completed"
    assert reaped.next_run_at is None


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


def _agent() -> str:
    import uuid

    return f"agent-{uuid.uuid4().hex[:8]}"


async def _make_due(session_factory, schedule_id) -> None:
    async with session_factory() as db:
        await db.execute(
            text("UPDATE scheduled_sessions SET next_run_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": schedule_id},
        )
        await db.commit()


async def _first_run(session_factory, redis_client, **kw):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    agent = _agent()
    schedule = await scheduled_store.create(
        org_id=org_id, user_id=user_id, agent_id=agent, name="Digest", prompt="send the digest",
        schedule=parse_schedule("10m"), source="tool", created_from_session_id=None,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1), **kw,
    )
    deps = _deps(session_factory, redis_client, scheduled_store, session_store)
    claimed = (await scheduled_store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0]
    run_id = await materialize_scheduled_run(claimed, **deps)
    return org_id, user_id, agent, schedule, run_id, scheduled_store, session_store, deps


async def test_a_run_is_told_which_schedule_and_run_it_is(session_factory, redis_client):
    _, _, agent, schedule, run_id, scheduled_store, session_store, deps = await _first_run(
        session_factory, redis_client,
    )
    identity = (await session_store.get_session(run_id)).config["scheduled_run"]
    assert identity["id"] == str(schedule.id)
    assert (identity["name"], identity["run_number"], identity["previous_run"]) == ("Digest", 1, None)
    assert identity["cadence"] == "Every 10 minutes"

    await session_store.update_session_status(run_id, "completed")
    await _make_due(session_factory, schedule.id)
    claimed = (await scheduled_store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0]
    second = await materialize_scheduled_run(claimed, **deps)
    identity = (await session_store.get_session(second)).config["scheduled_run"]
    assert identity["run_number"] == 2
    assert identity["previous_run"]["session_id"] == str(run_id)
    assert identity["previous_run"]["status"] == "completed"


async def test_no_new_run_while_the_previous_one_is_still_going(session_factory, redis_client):
    _, _, agent, schedule, run_id, scheduled_store, _, deps = await _first_run(session_factory, redis_client)
    await _make_due(session_factory, schedule.id)
    claimed = (await scheduled_store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0]

    assert await materialize_scheduled_run(claimed, **deps) == run_id

    row = await scheduled_store.get(schedule.id)
    assert (row.run_count, row.last_session_id, row.locked_by) == (1, run_id, None)
    assert "still going" in row.last_error
    assert row.next_run_at > datetime.now(timezone.utc)


async def test_a_run_past_the_run_limit_does_not_hold_its_schedule(session_factory, redis_client):
    _, _, agent, schedule, run_id, scheduled_store, _, deps = await _first_run(
        session_factory, redis_client, max_run_seconds=60,
    )
    async with session_factory() as db:
        await db.execute(
            text("UPDATE sessions SET created_at = now() - interval '2 minutes' WHERE id = :id"),
            {"id": run_id},
        )
        await db.commit()
    await _make_due(session_factory, schedule.id)
    claimed = (await scheduled_store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0]

    assert await materialize_scheduled_run(claimed, **deps) != run_id
    assert (await scheduled_store.get(schedule.id)).run_count == 2


async def test_cancelling_a_schedule_ends_its_running_run(session_factory, redis_client):
    org_id, user_id, agent, schedule, run_id, scheduled_store, session_store, _ = await _first_run(
        session_factory, redis_client,
    )
    event_id = (await session_store.get_events(run_id))[0].id
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO inbox_items (org_id, user_id, session_id, source_event_id, kind, title) "
                "VALUES (:org, :user, :run, :event, 'input_required', 'Which one?')"
            ),
            {"org": org_id, "user": user_id, "run": run_id, "event": event_id},
        )
        await db.commit()

    assert await scheduled_store.cancel(
        org_id=org_id, user_id=user_id, agent_id=agent, schedule_id=schedule.id, ended_by="user:x",
    )
    await recover_stalled_loops(
        scheduled_store=scheduled_store, redis=redis_client, session_store=session_store, limit=1000,
    )

    assert (await session_store.get_session(run_id)).status == "paused"
    async with session_factory() as db:
        status = (await db.execute(
            text("SELECT status FROM inbox_items WHERE session_id = :run"), {"run": run_id},
        )).scalar_one()
    assert status == "expired"


async def test_a_run_open_past_its_time_limit_is_failed_and_one_within_it_is_not(
    session_factory, redis_client,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    runs = {}
    for name in ("default", "long"):
        await scheduled_store.create(
            org_id=org_id, user_id=user_id, agent_id="agent-a", name=name,
            prompt="post", schedule=parse_schedule("10m"), source="loop",
            created_from_session_id=None,
            next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    async with session_factory() as db:
        await db.execute(
            text("UPDATE scheduled_sessions SET max_run_seconds = 14400 WHERE org_id = :o AND name = 'long'"),
            {"o": org_id},
        )
        await db.commit()
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)
    for row in await _claim_one(scheduled_store):
        runs[row.name] = await materialize_scheduled_run(
            row, **_deps(session_factory, redis_client, scheduled_store, session_store),
        )
    async with session_factory() as db:
        await db.execute(
            text("UPDATE sessions SET created_at = now() - interval '3 hours' WHERE id IN (:a, :b)"),
            {"a": runs["default"], "b": runs["long"]},
        )
        await db.commit()

    await recover_stalled_loops(
        scheduled_store=scheduled_store, redis=redis_client, session_store=session_store,
    )

    overdue = await session_store.get_session(runs["default"])
    within = await session_store.get_session(runs["long"])
    assert overdue.status == "failed"
    assert within.status == "active"
    fails = [
        e for e in await session_store.get_events(runs["default"])
        if e.type == EventType.SESSION_FAIL.value
    ]
    assert fails and fails[-1].data["reason"] == "run_time_limit"
    by_name = {
        row.name: row
        for row in await scheduled_store.list_for_user(org_id=org_id, user_id=user_id, agent_id="agent-a")
    }
    assert by_name["default"].last_error.startswith("The last run was stopped after 2 hours")
    assert by_name["default"].consecutive_failures == 1
    assert by_name["long"].last_error is None and by_name["long"].consecutive_failures == 0


async def test_a_schedule_that_ends_on_its_own_tells_its_chat_once(session_factory, redis_client):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    agent = _agent()
    chat = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=agent, channel="web",
        config={
            "storage_bucket": "agent-a-bucket", "storage_key_prefix": "",
            "workspace_path": "/workspace/agent-a-bucket/sessions/C",
        },
    )
    now = datetime.now(timezone.utc)
    limited = await scheduled_store.create(
        org_id=org_id, user_id=user_id, agent_id=agent, name="Twice", prompt="post",
        schedule=parse_schedule("10m"), source="tool", created_from_session_id=chat.id,
        repeat_limit=1, next_run_at=now - timedelta(minutes=1),
    )
    expired = await scheduled_store.create(
        org_id=org_id, user_id=user_id, agent_id=agent, name="Week", prompt="post",
        schedule=parse_schedule("10m"), source="loop", created_from_session_id=chat.id,
        next_run_at=now + timedelta(hours=1), expires_at=now - timedelta(minutes=1),
    )
    cancelled = await scheduled_store.create(
        org_id=org_id, user_id=user_id, agent_id=agent, name="Mine", prompt="post",
        schedule=parse_schedule("10m"), source="tool", created_from_session_id=chat.id,
    )
    await scheduled_store.cancel(
        org_id=org_id, user_id=user_id, agent_id=agent, schedule_id=cancelled.id, ended_by="user:x",
    )
    deps = _deps(session_factory, redis_client, scheduled_store, session_store)
    [claimed] = await scheduled_store.claim_due(agent_id=agent, worker_id="w1", limit=5)
    await materialize_scheduled_run(claimed, **deps)

    for _ in range(2):
        await recover_stalled_loops(
            scheduled_store=scheduled_store, redis=redis_client, session_store=session_store, limit=1000,
        )

    notices = {
        e.data["scheduled_session_id"]: e.data
        for e in await session_store.get_events(chat.id)
        if e.type == EventType.LOOP_RESULT.value
    }
    assert set(notices) == {str(limited.id), str(expired.id)}
    assert notices[str(limited.id)]["ended_reason"] == "limit"
    assert "ran all 1 times" in notices[str(limited.id)]["content"]
    assert notices[str(expired.id)]["outcome"] == "schedule_ended"
    assert "reached its end date" in notices[str(expired.id)]["content"]
    assert len([
        e for e in await session_store.get_events(chat.id) if e.type == EventType.LOOP_RESULT.value
    ]) == 2


async def test_a_failed_schedule_says_why_it_stopped(session_factory, redis_client):
    from surogates.scheduled.materialize import ending_notice

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    agent = _agent()
    schedule = await scheduled_store.create(
        org_id=org_id, user_id=user_id, agent_id=agent, name="Digest", prompt="post",
        schedule=parse_schedule("10m"), source="tool", created_from_session_id=None,
    )
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE scheduled_sessions SET status = 'failed', ended_reason = 'failed', "
                "last_error = 'login rejected', run_count = 3 WHERE id = :id"
            ),
            {"id": schedule.id},
        )
        await db.commit()
    [claimed] = [s for s in await scheduled_store.claim_unannounced_endings(limit=1000) if s.id == schedule.id]
    assert ending_notice(claimed) == (
        'The schedule "Digest" has stopped: its runs kept failing. The last error: login rejected. '
        "It ran 3 times. Fix the cause, then make it again."
    )
