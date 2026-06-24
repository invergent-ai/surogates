import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from surogates.db.models import AmbientScheduleRow, Base
from surogates.ambient.store import AmbientScheduleStore


def test_table_and_columns():
    t = AmbientScheduleRow.__table__
    assert t.name == "ambient_schedules"
    cols = set(t.columns.keys())
    assert {
        "id", "org_id", "agent_id", "platform", "channel_id",
        "ambient_session_id", "cadence_seconds", "status",
        "next_run_at", "locked_by", "locked_until",
        "created_at", "updated_at",
    } <= cols


@pytest.fixture
async def store():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    # Create only this table (the rest of the surogates metadata uses Postgres
    # dialect types that don't render on SQLite); the model's .with_variant
    # makes ambient_schedules portable.
    async with engine.begin() as conn:
        await conn.run_sync(AmbientScheduleRow.__table__.create)
    yield AmbientScheduleStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_is_idempotent(store):
    org = uuid.uuid4()
    a = await store.ensure(
        org_id=org, agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=1800, config={},
    )
    b = await store.ensure(
        org_id=org, agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=900, config={},
    )
    assert a.id == b.id  # same row, not a duplicate


@pytest.mark.asyncio
async def test_claim_due_returns_active_past_due(store):
    org = uuid.uuid4()
    await store.ensure(
        org_id=org, agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=0, config={},  # next_run_at = now
    )
    due = await store.claim_due(worker_id="w1", limit=10)
    assert len(due) == 1
    assert due[0].channel_id == "C1"


@pytest.mark.asyncio
async def test_mark_fired_advances_and_sets_session(store):
    org = uuid.uuid4()
    sched = await store.ensure(
        org_id=org, agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=0, config={},
    )
    sid = uuid.uuid4()
    await store.mark_fired(sched, ambient_session_id=sid)
    again = await store.claim_due(worker_id="w1", limit=10)
    assert again[0].ambient_session_id == sid


@pytest.mark.asyncio
async def test_deactivate_pauses(store):
    org = uuid.uuid4()
    sched = await store.ensure(
        org_id=org, agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=0, config={},
    )
    await store.deactivate(sched.id)
    assert await store.claim_due(worker_id="w1", limit=10) == []
