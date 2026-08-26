"""The ``kind`` retrofit, exercised against a pre-``kind`` database.

``create_all`` builds the new shape on a fresh database, so the fixture-backed
tests never touch the path every existing deployment will actually take. This
module downgrades a real table back to the old schema, replays the real
``observability.sql``, and asserts the outcome — including that replaying it a
second time changes nothing.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import Base
from surogates.tenant.auth.service_account import (
    KIND_AGENT_PRINCIPAL,
    KIND_API_KEY,
    KIND_SERVICE,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def retrofit_engine(pg_url):
    """A database of this module's own, on the shared container.

    ``_downgrade`` re-creates the pre-``kind`` unique index over the WHOLE
    table, so it cannot run against the database the rest of the suite uses:
    any sibling test that gave one agent two API keys makes that index
    impossible to build, and this module would fail for a reason that has
    nothing to do with the retrofit. An isolated database keeps the downgrade
    honest while still exercising the real script.
    """
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    db_name = f"retrofit_{uuid.uuid4().hex[:12]}"
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await admin.dispose()

    url = pg_url.rsplit("/", 1)[0] + f"/{db_name}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
        admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        await admin.dispose()


async def _downgrade(conn) -> None:
    """Put ``service_accounts`` back the way it looked before ``kind``."""
    await conn.execute(text("DROP INDEX IF EXISTS uq_service_accounts_agent_principal"))
    await conn.execute(text("DROP INDEX IF EXISTS idx_service_accounts_agent"))
    await conn.execute(text("ALTER TABLE service_accounts DROP COLUMN IF EXISTS kind"))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_service_accounts_agent "
        "ON service_accounts (agent_id) WHERE agent_id IS NOT NULL"
    ))


async def _indexes(conn) -> set[str]:
    rows = await conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'service_accounts'"
    ))
    return {r[0] for r in rows}


async def _insert(conn, *, org_id, name, agent_id, kind=None):
    # ``id`` is defaulted in Python by the ORM, not by the table built via
    # ``create_all`` — raw SQL has to supply it.
    cols = "id, org_id, name, token_hash, token_prefix, agent_id"
    vals = ":id, :org_id, :name, :token_hash, :prefix, :agent_id"
    params = {
        "id": uuid.uuid4(),
        "org_id": org_id,
        "name": name,
        "token_hash": uuid.uuid4().hex,
        "prefix": "surg_sk_test",
        "agent_id": agent_id,
    }
    if kind is not None:
        cols += ", kind"
        vals += ", :kind"
        params["kind"] = kind
    await conn.execute(
        text(f"INSERT INTO service_accounts ({cols}) VALUES ({vals})"), params,
    )


async def test_retrofit_backfills_kind_and_swaps_the_index(retrofit_engine):
    org_id = uuid.uuid4()
    agent_id = f"agent-{uuid.uuid4()}"

    async with retrofit_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, :n)"),
            {"id": org_id, "n": f"retrofit-{org_id}"},
        )
        await _downgrade(conn)

        # Rows exactly as a pre-``kind`` deployment would hold them.
        await _insert(conn, org_id=org_id, name="principal", agent_id=agent_id)
        await _insert(conn, org_id=org_id, name="pipeline", agent_id=None)

        before = await _indexes(conn)
        assert "uq_service_accounts_agent" in before
        assert "uq_service_accounts_agent_principal" not in before

    # --- the real retrofit ------------------------------------------------
    async with retrofit_engine.begin() as conn:
        await apply_observability_ddl(conn)

    async with retrofit_engine.begin() as conn:
        after = await _indexes(conn)
        assert "uq_service_accounts_agent" not in after, (
            "the old cap-at-one index must be dropped, or a second API key "
            "still collides"
        )
        assert "uq_service_accounts_agent_principal" in after
        assert "idx_service_accounts_agent" in after

        kinds = dict((await conn.execute(text(
            "SELECT name, kind FROM service_accounts WHERE org_id = :o"
        ), {"o": org_id})).all())
        assert kinds["principal"] == KIND_AGENT_PRINCIPAL, (
            "a row that carried agent_id before `kind` existed could only "
            "have been the agent principal"
        )
        assert kinds["pipeline"] == KIND_SERVICE


async def test_retrofit_is_idempotent_and_does_not_relabel_api_keys(retrofit_engine):
    """Replaying the script must not sweep live API keys into principals."""
    org_id = uuid.uuid4()
    agent_id = f"agent-{uuid.uuid4()}"

    async with retrofit_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, :n)"),
            {"id": org_id, "n": f"idem-{org_id}"},
        )
        await _insert(
            conn, org_id=org_id, name="principal", agent_id=agent_id,
            kind=KIND_AGENT_PRINCIPAL,
        )
        await _insert(
            conn, org_id=org_id, name="customer-key", agent_id=agent_id,
            kind=KIND_API_KEY,
        )

    for _ in range(2):
        async with retrofit_engine.begin() as conn:
            await apply_observability_ddl(conn)

    async with retrofit_engine.begin() as conn:
        kinds = dict((await conn.execute(text(
            "SELECT name, kind FROM service_accounts WHERE org_id = :o"
        ), {"o": org_id})).all())
    assert kinds == {
        "principal": KIND_AGENT_PRINCIPAL,
        "customer-key": KIND_API_KEY,
    }, (
        "the backfill predicate must settle to a no-op: an API key relabelled "
        "as a principal would collide with the unique index and take the "
        "agent's real identity out of resolution"
    )


async def test_after_the_retrofit_a_second_api_key_inserts(retrofit_engine):
    """The whole point of the change, asserted at the database level."""
    org_id = uuid.uuid4()
    agent_id = f"agent-{uuid.uuid4()}"

    # Deliberately no ``_downgrade`` here: once ANY agent in the table holds
    # more than one key, the old index can no longer be created at all
    # ("could not create unique index"). That is the constraint this change
    # removes, and re-imposing it mid-suite would only re-prove it. The
    # downgrade path itself is covered by the first test in this module.
    async with retrofit_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, :n)"),
            {"id": org_id, "n": f"multi-{org_id}"},
        )
        await _insert(
            conn, org_id=org_id, name="principal", agent_id=agent_id,
            kind=KIND_AGENT_PRINCIPAL,
        )
        await apply_observability_ddl(conn)

    async with retrofit_engine.begin() as conn:
        for i in range(3):
            await _insert(
                conn, org_id=org_id, name=f"key-{i}", agent_id=agent_id,
                kind=KIND_API_KEY,
            )
        n = (await conn.execute(text(
            "SELECT count(*) FROM service_accounts "
            "WHERE agent_id = :a AND kind = :k"
        ), {"a": agent_id, "k": KIND_API_KEY})).scalar_one()
    assert n == 3

    # ...and the principal is still unique.
    with pytest.raises(Exception):
        async with retrofit_engine.begin() as conn:
            await _insert(
                conn, org_id=org_id, name="principal-2", agent_id=agent_id,
                kind=KIND_AGENT_PRINCIPAL,
            )


async def test_two_pre_kind_principals_for_one_agent_do_not_break_the_script(
    retrofit_engine,
):
    """A rolling deploy can leave an agent holding two pre-``kind`` rows.

    A replica still running the old code writes ``agent_id`` with the column
    default, which the new partial index does not constrain. Promoting BOTH
    would make the unique index impossible to build and abort the whole
    script inside its transaction — taking the service down on next start.
    """
    org_id = uuid.uuid4()
    agent_id = f"agent-{uuid.uuid4()}"

    async with retrofit_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, :n)"),
            {"id": org_id, "n": f"race-{org_id}"},
        )
        # Both written by pre-``kind`` code: agent_id set, kind at its default.
        await _insert(conn, org_id=org_id, name="old", agent_id=agent_id)
        await _insert(conn, org_id=org_id, name="new", agent_id=agent_id)

    # Must not raise.
    async with retrofit_engine.begin() as conn:
        await apply_observability_ddl(conn)

    async with retrofit_engine.begin() as conn:
        kinds = sorted(
            r[0] for r in (await conn.execute(text(
                "SELECT kind FROM service_accounts WHERE agent_id = :a"
            ), {"a": agent_id})).all()
        )
        indexes = await _indexes(conn)

    assert kinds.count(KIND_AGENT_PRINCIPAL) == 1, (
        f"exactly one row may be promoted, got {kinds}"
    )
    assert "uq_service_accounts_agent_principal" in indexes, (
        "the unique index must still build"
    )

    # Replaying stays a no-op.
    async with retrofit_engine.begin() as conn:
        await apply_observability_ddl(conn)
