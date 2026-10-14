"""The step funnel runs on Postgres, which refused its grouping once."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db.models import Base, ProgramInvitationRow
from surogates.programs.funnel import step_funnel

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def engine(pg_url):
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    name = f"funnel_{uuid.uuid4().hex[:12]}"
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(pg_url.rsplit("/", 1)[0] + f"/{name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_the_funnel_counts_on_postgres(engine):
    sf = async_sessionmaker(engine, expire_on_commit=False)
    org, step = uuid.uuid4(), uuid.uuid4()

    def row(**extra):
        return ProgramInvitationRow(
            occurrence_id=uuid.uuid4(), program_id=step, org_id=org, agent_id="a1",
            user_id=uuid.uuid4(), platform="whatsapp", platform_user_id="1", **extra,
        )

    async with sf() as db:
        db.add_all([
            row(delivery_state="skipped", reason="No reachable channel"),
            row(delivery_state="skipped", reason="No reachable channel"),
            row(delivery_state="delivered", response_state="completed", outcome="red",
                read_at=datetime.now(timezone.utc), reason="a note"),
        ])
        await db.commit()
    [funnel] = (await step_funnel(
        sf, org_id=org, agent_id="a1", program_ids=[step],
        since=datetime.now(timezone.utc) - timedelta(days=1),
    )).values()
    assert (funnel["invited"], funnel["skipped"], funnel["read"], funnel["completed"]) == (3, 2, 1, 1)
    assert funnel["skip_reasons"] == {"No reachable channel": 2}
    assert funnel["outcomes"] == {"red": 1}
