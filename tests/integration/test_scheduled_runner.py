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
    origin = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="agent-a",
        channel="web",
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
