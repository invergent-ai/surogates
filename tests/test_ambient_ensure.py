import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from surogates.db.models import AmbientScheduleRow
from surogates.ambient.store import AmbientScheduleStore
from surogates.ambient.reconcile import reconcile_ambient_schedule


@pytest.fixture
async def store():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(AmbientScheduleRow.__table__.create)
    yield AmbientScheduleStore(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


@pytest.mark.asyncio
async def test_enables_creates_schedule_with_caps(store):
    org = uuid.uuid4()
    await reconcile_ambient_schedule(
        store,
        settings_dict={
            "ambient_enabled": True, "ambient_cadence_seconds": 900,
            "confidence_threshold": 0.8, "max_proactive_posts_per_day": 3,
        },
        org_id=org, agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, team_id="T1",
    )
    got = await store.get(agent_id="ag", platform="slack", channel_id="C1")
    assert got is not None
    assert got.cadence_seconds == 900
    assert got.config["ambient_caps"]["confidence_threshold"] == 0.8
    assert got.config["slack_team_id"] == "T1"


@pytest.mark.asyncio
async def test_disables_deactivates_schedule(store):
    org = uuid.uuid4()
    await store.ensure(
        org_id=org, agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, cadence_seconds=900, config={},
    )
    await reconcile_ambient_schedule(
        store, settings_dict={"ambient_enabled": False},
        org_id=org, agent_id="ag", platform="slack", channel_id="C1",
        source_session_id=None, team_id="T1",
    )
    got = await store.get(agent_id="ag", platform="slack", channel_id="C1")
    assert got.status == "paused"
