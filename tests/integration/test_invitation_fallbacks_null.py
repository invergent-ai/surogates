"""An invitation without fallbacks stores SQL NULL, which every query tests
for; rows written as JSON null before are rewritten by the retrofit."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import Base, ProgramInvitationRow

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def engine(pg_url):
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    name = f"fallbacks_{uuid.uuid4().hex[:12]}"
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


def _row(**extra):
    return ProgramInvitationRow(
        occurrence_id=uuid.uuid4(), program_id=uuid.uuid4(), org_id=uuid.uuid4(),
        agent_id="a1", user_id=uuid.uuid4(), platform="whatsapp", platform_user_id="1",
        **extra,
    )


async def test_no_fallbacks_is_sql_null(engine):
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as db:
        cleared = _row(fallback_platforms=["telegram"], attempts=[{"platform": "x"}])
        db.add_all([_row(fallback_platforms=None, attempts=None), cleared])
        await db.commit()
        cleared.fallback_platforms = cleared.attempts = None
        await db.commit()
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO program_invitations (id, occurrence_id, program_id, org_id, agent_id,"
            " user_id, platform, platform_user_id, fallback_platforms, attempts)"
            " VALUES (:id, :id, :id, :id, 'a1', :id, 'whatsapp', '1', 'null'::jsonb, 'null'::jsonb)"
        ), {"id": uuid.uuid4()})
        await apply_observability_ddl(conn)
        rows = (await conn.execute(text(
            "SELECT fallback_platforms IS NULL AND attempts IS NULL FROM program_invitations"
        ))).scalars().all()
    assert rows == [True, True, True]
