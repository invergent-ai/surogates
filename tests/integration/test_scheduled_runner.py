from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from surogates.config import agent_queue_key
from surogates.scheduled.runner import ScheduledSessionRunner
from surogates.scheduled.schedule import parse_dynamic_loop_schedule, parse_schedule
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session.events import EventType
from surogates.session.store import SessionStore

from .conftest import create_org, create_user

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

    class llm:
        model = "gpt-4o"

    class scheduled_sessions:
        claim_limit = 10
        claim_lease_seconds = 120


async def test_runner_creates_user_session_and_enqueues(session_factory, redis_client):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
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

    queue = agent_queue_key("agent-a")
    await redis_client.delete(queue)
    session_store = SessionStore(session_factory, redis=redis_client)
    runner = ScheduledSessionRunner(
        settings=FakeSettings(),
        session_factory=session_factory,
        session_store=session_store,
        scheduled_store=scheduled_store,
        redis=redis_client,
        storage=FakeStorage(),
    )

    processed = await runner.tick_once()
    assert processed == 1

    updated = await scheduled_store.get(schedule.id)
    assert updated.last_session_id is not None
    assert updated.run_count == 1

    queued = await redis_client.zscore(queue, str(updated.last_session_id))
    assert queued is not None

    events = await session_store.get_events(updated.last_session_id)
    assert events[0].type == EventType.USER_MESSAGE.value
    assert events[0].data["content"] == "/status check deployment"


async def test_runner_marks_dynamic_loop_sessions(session_factory, redis_client):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    schedule = await scheduled_store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        prompt="check CI",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=None,
    )

    queue = agent_queue_key("agent-a")
    await redis_client.delete(queue)
    session_store = SessionStore(session_factory, redis=redis_client)
    runner = ScheduledSessionRunner(
        settings=FakeSettings(),
        session_factory=session_factory,
        session_store=session_store,
        scheduled_store=scheduled_store,
        redis=redis_client,
        storage=FakeStorage(),
    )

    processed = await runner.tick_once()
    assert processed == 1

    updated = await scheduled_store.get(schedule.id)
    session = await session_store.get_session(updated.last_session_id)
    assert session.config["scheduled_session_id"] == str(schedule.id)
    assert session.config["scheduled_dynamic_loop"] is True


async def test_runner_links_scheduled_run_to_origin_session(
    session_factory,
    redis_client,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    # Origin must carry workspace config — production parents always
    # come through create_agent_session(), which seeds these.
    origin = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="agent-a",
        channel="web",
        config={
            "storage_bucket": "agent-a-bucket",
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

    queue = agent_queue_key("agent-a")
    await redis_client.delete(queue)
    runner = ScheduledSessionRunner(
        settings=FakeSettings(),
        session_factory=session_factory,
        session_store=session_store,
        scheduled_store=scheduled_store,
        redis=redis_client,
        storage=FakeStorage(),
    )

    processed = await runner.tick_once()

    assert processed == 1
    updated = await scheduled_store.get(schedule.id)
    child = await session_store.get_session(updated.last_session_id)
    assert child.parent_id == origin.id


async def test_runner_child_session_inherits_creator_workspace(
    session_factory,
    redis_client,
):
    """A scheduled run with a known creator must share the creator's workspace.

    Two iterations of the same dynamic loop must both land on the
    creator's storage_bucket + workspace_path, and both resolve to the
    creator session via ``sandbox_session_key()``.
    """
    from surogates.sandbox.pool import sandbox_session_key

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    scheduled_store = ScheduledSessionStore(session_factory)
    session_store = SessionStore(session_factory, redis=redis_client)
    # The creator owns the workspace.
    creator_workspace = "/workspace/agent-a-bucket/sessions/CREATOR"
    creator = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="agent-a",
        channel="web",
        config={
            "storage_bucket": "agent-a-bucket",
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

    queue = agent_queue_key("agent-a")
    await redis_client.delete(queue)
    runner = ScheduledSessionRunner(
        settings=FakeSettings(),
        session_factory=session_factory,
        session_store=session_store,
        scheduled_store=scheduled_store,
        redis=redis_client,
        storage=FakeStorage(),
    )

    # First tick → first child session.
    processed = await runner.tick_once()
    assert processed == 1
    first_schedule = await scheduled_store.get(schedule.id)
    first_child = await session_store.get_session(first_schedule.last_session_id)
    assert first_child.parent_id == creator.id
    assert first_child.config["storage_bucket"] == "agent-a-bucket"
    assert first_child.config["workspace_path"] == creator_workspace
    assert first_child.config["sandbox_root_session_id"] == str(creator.id)
    assert sandbox_session_key(first_child) == str(creator.id)

    # Force the schedule due again and run a second tick.
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE scheduled_sessions SET next_run_at = now() - "
                "make_interval(secs => 60) WHERE id = :sid"
            ),
            {"sid": schedule.id},
        )
        await db.commit()

    processed = await runner.tick_once()
    assert processed == 1
    second_schedule = await scheduled_store.get(schedule.id)
    second_child = await session_store.get_session(second_schedule.last_session_id)
    assert second_child.id != first_child.id
    assert second_child.config["workspace_path"] == creator_workspace
    # Both iterations resolve to the SAME root via the shared key.
    assert sandbox_session_key(second_child) == sandbox_session_key(first_child)


async def test_runner_falls_back_to_fresh_workspace_when_creator_missing(
    session_factory,
    redis_client,
):
    """A schedule whose creator cannot be loaded gets a fresh workspace.

    The runner must catch ``SessionNotFoundError`` and route through
    ``create_agent_session`` so the schedule continues to fire instead
    of crashing.  We model "creator gone" by pointing the schedule at a
    UUID that has no matching ``sessions`` row.
    """
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

    queue = agent_queue_key("agent-a")
    await redis_client.delete(queue)
    runner = ScheduledSessionRunner(
        settings=FakeSettings(),
        session_factory=session_factory,
        session_store=session_store,
        scheduled_store=scheduled_store,
        redis=redis_client,
        storage=FakeStorage(),
    )

    processed = await runner.tick_once()
    assert processed == 1

    updated = await scheduled_store.get(schedule.id)
    child = await session_store.get_session(updated.last_session_id)
    # Fresh workspace path keyed on the child's own id (the fallback
    # path through ``create_agent_session``).
    assert child.config["workspace_path"] == (
        f"/workspace/agent-a-bucket/sessions/{child.id}"
    )
    # No sandbox_root_session_id is stamped on the detached path.
    assert "sandbox_root_session_id" not in child.config


async def test_runner_requeues_stalled_dynamic_loop_sessions(
    session_factory,
    redis_client,
    monkeypatch,
):
    monkeypatch.setattr(
        "surogates.scheduled.runner.DYNAMIC_LOOP_STALE_RUN_SECONDS",
        1,
    )
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
        agent_id="agent-a",
        worker_id="w1",
        limit=1,
    ))[0]
    await scheduled_store.mark_run_created(claimed, session_id=run_session.id)

    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE sessions SET updated_at = now() - make_interval(secs => 10) "
                "WHERE id = :sid"
            ),
            {"sid": run_session.id},
        )
        await db.commit()

    queue = agent_queue_key("agent-a")
    await redis_client.delete(queue)
    runner = ScheduledSessionRunner(
        settings=FakeSettings(),
        session_factory=session_factory,
        session_store=session_store,
        scheduled_store=scheduled_store,
        redis=redis_client,
        storage=FakeStorage(),
    )

    processed = await runner.tick_once()

    assert processed == 0
    score = await redis_client.zscore(queue, str(run_session.id))
    assert score is not None
    updated = await scheduled_store.get(schedule.id)
    assert updated.next_run_at is None
