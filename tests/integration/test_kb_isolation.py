"""KB tenant isolation tests.

Exercises the row-level-security policies in ``surogates/db/kb.sql`` end-
to-end. Tests connect as a non-superuser role (``kb_app_test``) created
on demand; the testcontainers default role is a superuser and would
bypass RLS entirely (FORCE doesn't help superusers — see comment in
``kb.sql``).

For each KB table we verify three invariants:

  1. **Read with platform**: queries see the tenant's own rows AND
     platform rows (``org_id IS NULL``); rows from other tenants are
     hidden.
  2. **Write strictly own**: INSERT, UPDATE, DELETE only succeed against
     own-tenant rows. Platform writes from the app role are rejected
     (platform content is seeded by a privileged migration role that
     bypasses RLS).
  3. **No GUC = platform-only read**: queries without ``app.org_id``
     set see only platform rows and write nothing.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import create_org

# Bind every test to the session-scoped event loop (matches the engine /
# session_factory fixtures in conftest).
pytestmark = pytest.mark.asyncio(loop_scope="session")


# A literal we treat as "the test role". Created once per test session
# in the ``kb_app_role`` fixture below.
TEST_ROLE = "kb_app_test"


# ---------------------------------------------------------------------------
# Session-scoped role fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def kb_app_role(engine):
    """Create a non-superuser role with DML perms on the KB tables.

    Why: testcontainers' default Postgres user is a superuser and
    bypasses RLS entirely. ``FORCE ROW LEVEL SECURITY`` only helps with
    table owners who are NOT superusers. So we add a real non-superuser
    role and exercise RLS via ``SET LOCAL ROLE``.

    Idempotent on reruns — ``CREATE ROLE`` is wrapped in a DO block.
    """
    async with engine.begin() as conn:
        # Multi-statement script via simple-query protocol (same trick as
        # apply_observability_ddl in db/engine.py) — exec_driver_sql goes
        # through the extended protocol and rejects multi-statement input.
        raw = await conn.get_raw_connection()
        await raw.driver_connection.execute(
            f"""
            DO $$ BEGIN
                CREATE ROLE {TEST_ROLE} NOLOGIN NOBYPASSRLS;
            EXCEPTION WHEN duplicate_object THEN null;
            END $$;
            GRANT USAGE ON SCHEMA public TO {TEST_ROLE};
            GRANT SELECT, INSERT, UPDATE, DELETE ON
                kb, kb_source, kb_raw_doc, kb_wiki_entry, kb_chunk, agent_kb_grant
                TO {TEST_ROLE};
            -- Tests need to read parent tables for FK + lookup; SELECT only.
            GRANT SELECT ON orgs, agents TO {TEST_ROLE};
            """
        )
    yield TEST_ROLE
    # Container is torn down with the session; nothing else to do.


@asynccontextmanager
async def as_tenant(
    db: AsyncSession,
    org_id: uuid.UUID | None,
) -> AsyncIterator[None]:
    """Run the enclosed body as the ``kb_app_test`` role with
    ``app.org_id`` set to *org_id* (or unset when ``None``).

    Both settings are scoped to the active transaction via ``SET LOCAL``
    so they unwind cleanly when the caller commits or rolls back.
    """
    if org_id is None:
        await db.execute(text("RESET app.org_id"))
    else:
        await db.execute(text(f"SET LOCAL app.org_id = '{org_id}'"))
    await db.execute(text(f"SET LOCAL ROLE {TEST_ROLE}"))
    try:
        yield
    finally:
        # Roles unwind automatically on transaction end; explicit reset
        # so tests that don't await commit/rollback don't leak the role
        # into a follow-up query within the same session.
        try:
            await db.execute(text("RESET ROLE"))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Seed helpers (run as superuser to bypass RLS for setup)
# ---------------------------------------------------------------------------


async def _seed_kb(
    session_factory: async_sessionmaker,
    org_id: uuid.UUID | None,
    name: str | None = None,
) -> uuid.UUID:
    kb_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md, is_platform) "
                "VALUES (:id, :org_id, :name, '', :is_platform)"
            ),
            {
                "id": kb_id,
                "org_id": org_id,
                "name": name or f"kb-{kb_id}",
                "is_platform": org_id is None,
            },
        )
        await db.commit()
    return kb_id


async def _seed_wiki_entry(
    session_factory: async_sessionmaker,
    kb_id: uuid.UUID,
) -> uuid.UUID:
    entry_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb_wiki_entry "
                "(id, kb_id, path, kind, content_sha) "
                "VALUES (:id, :kb_id, :path, 'summary', 'sha')"
            ),
            {
                "id": entry_id,
                "kb_id": kb_id,
                "path": f"wiki/summaries/{entry_id}.md",
            },
        )
        await db.commit()
    return entry_id


# ---------------------------------------------------------------------------
# Role plumbing
# ---------------------------------------------------------------------------


async def test_test_role_is_non_superuser(kb_app_role, session_factory):
    """The whole test suite is meaningless if the test role accidentally
    has superuser or BYPASSRLS attributes. Sanity-check the role itself.
    """
    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = :n"
                ),
                {"n": kb_app_role},
            )
        ).first()
    assert row is not None
    assert row.rolsuper is False
    assert row.rolbypassrls is False


# ---------------------------------------------------------------------------
# Read isolation on `kb`
# ---------------------------------------------------------------------------


async def test_no_gut_sees_only_platform_kbs(kb_app_role, session_factory):
    org = await create_org(session_factory)
    own_kb = await _seed_kb(session_factory, org, name=f"own-{uuid.uuid4()}")
    plat_kb = await _seed_kb(session_factory, None, name=f"plat-{uuid.uuid4()}")

    async with session_factory() as db:
        async with as_tenant(db, None):
            ids = {
                r.id
                for r in (
                    await db.execute(text("SELECT id FROM kb"))
                ).all()
            }
    assert plat_kb in ids
    assert own_kb not in ids


async def test_matching_gut_sees_own_and_platform(kb_app_role, session_factory):
    org = await create_org(session_factory)
    own_kb = await _seed_kb(session_factory, org, name=f"own-{uuid.uuid4()}")
    plat_kb = await _seed_kb(session_factory, None, name=f"plat-{uuid.uuid4()}")

    async with session_factory() as db:
        async with as_tenant(db, org):
            ids = {
                r.id
                for r in (
                    await db.execute(text("SELECT id FROM kb"))
                ).all()
            }
    assert plat_kb in ids
    assert own_kb in ids


async def test_mismatched_gut_does_not_see_other_org_kb(
    kb_app_role, session_factory
):
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    a_kb = await _seed_kb(session_factory, org_a, name=f"a-{uuid.uuid4()}")
    b_kb = await _seed_kb(session_factory, org_b, name=f"b-{uuid.uuid4()}")

    async with session_factory() as db:
        async with as_tenant(db, org_a):
            ids = {
                r.id
                for r in (
                    await db.execute(text("SELECT id FROM kb"))
                ).all()
            }
    assert a_kb in ids
    assert b_kb not in ids, "RLS leak: org A saw org B's KB"


# ---------------------------------------------------------------------------
# Write isolation on `kb`
# ---------------------------------------------------------------------------


async def test_app_role_cannot_insert_kb_for_other_org(
    kb_app_role, session_factory
):
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)

    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as db:
            async with as_tenant(db, org_a):
                await db.execute(
                    text(
                        "INSERT INTO kb (id, org_id, name, agents_md) "
                        "VALUES (:id, :org_id, :name, '')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org_id": org_b,
                        "name": f"cross-{uuid.uuid4()}",
                    },
                )
                await db.commit()
    assert "row-level security" in str(exc_info.value).lower() or \
           "violates row-level security" in str(exc_info.value).lower()


async def test_app_role_cannot_insert_platform_kb(
    kb_app_role, session_factory
):
    org = await create_org(session_factory)

    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as db:
            async with as_tenant(db, org):
                await db.execute(
                    text(
                        "INSERT INTO kb (id, org_id, name, agents_md, is_platform) "
                        "VALUES (:id, NULL, :name, '', true)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "name": f"plat-{uuid.uuid4()}",
                    },
                )
                await db.commit()
    assert "row-level security" in str(exc_info.value).lower()


async def test_app_role_update_other_org_affects_zero_rows(
    kb_app_role, session_factory
):
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    b_kb = await _seed_kb(session_factory, org_b, name=f"b-{uuid.uuid4()}")

    async with session_factory() as db:
        async with as_tenant(db, org_a):
            result = await db.execute(
                text(
                    "UPDATE kb SET description = 'pwned' WHERE id = :id"
                ),
                {"id": b_kb},
            )
            await db.commit()
            assert result.rowcount == 0

    # Verify B's row is unchanged when read as superuser.
    async with session_factory() as db:
        row = (
            await db.execute(
                text("SELECT description FROM kb WHERE id = :id"),
                {"id": b_kb},
            )
        ).first()
    assert row is not None
    assert row.description != "pwned"


async def test_app_role_cannot_change_own_kb_org_id_to_other_org(
    kb_app_role, session_factory
):
    """WITH CHECK predicate prevents moving a row out of the tenant's
    scope. Trying to UPDATE org_id to another org's id is rejected.
    """
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    a_kb = await _seed_kb(session_factory, org_a, name=f"a-{uuid.uuid4()}")

    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as db:
            async with as_tenant(db, org_a):
                await db.execute(
                    text("UPDATE kb SET org_id = :nb WHERE id = :id"),
                    {"nb": org_b, "id": a_kb},
                )
                await db.commit()
    assert "row-level security" in str(exc_info.value).lower()


async def test_app_role_cannot_delete_platform_kb(
    kb_app_role, session_factory
):
    plat_kb = await _seed_kb(session_factory, None, name=f"plat-{uuid.uuid4()}")
    org = await create_org(session_factory)

    async with session_factory() as db:
        async with as_tenant(db, org):
            result = await db.execute(
                text("DELETE FROM kb WHERE id = :id"),
                {"id": plat_kb},
            )
            await db.commit()
            assert result.rowcount == 0

    # Platform KB still there.
    async with session_factory() as db:
        row = (
            await db.execute(
                text("SELECT 1 FROM kb WHERE id = :id"),
                {"id": plat_kb},
            )
        ).first()
    assert row is not None


# ---------------------------------------------------------------------------
# Isolation on child tables (kb_source, kb_raw_doc, kb_wiki_entry)
# ---------------------------------------------------------------------------


async def test_kb_source_isolated_via_parent_kb(kb_app_role, session_factory):
    """A kb_source row's visibility follows its parent kb's visibility."""
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    a_kb = await _seed_kb(session_factory, org_a, name=f"a-{uuid.uuid4()}")
    b_kb = await _seed_kb(session_factory, org_b, name=f"b-{uuid.uuid4()}")

    a_src = uuid.uuid4()
    b_src = uuid.uuid4()
    async with session_factory() as db:
        for sid, kid in [(a_src, a_kb), (b_src, b_kb)]:
            await db.execute(
                text(
                    "INSERT INTO kb_source (id, kb_id, kind, config) "
                    "VALUES (:id, :kb_id, 'markdown_dir', '{}')"
                ),
                {"id": sid, "kb_id": kid},
            )
        await db.commit()

    async with session_factory() as db:
        async with as_tenant(db, org_a):
            ids = {
                r.id
                for r in (
                    await db.execute(text("SELECT id FROM kb_source"))
                ).all()
            }
    assert a_src in ids
    assert b_src not in ids


async def test_app_role_cannot_insert_kb_source_for_other_org_kb(
    kb_app_role, session_factory
):
    """Even with INSERT privilege on kb_source, the strict_own WITH
    CHECK prevents pointing a new source at another org's kb.
    """
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    b_kb = await _seed_kb(session_factory, org_b, name=f"b-{uuid.uuid4()}")

    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as db:
            async with as_tenant(db, org_a):
                await db.execute(
                    text(
                        "INSERT INTO kb_source (id, kb_id, kind, config) "
                        "VALUES (:id, :kb_id, 'markdown_dir', '{}')"
                    ),
                    {"id": uuid.uuid4(), "kb_id": b_kb},
                )
                await db.commit()
    assert "row-level security" in str(exc_info.value).lower()


async def test_kb_wiki_entry_isolated_via_parent_kb(
    kb_app_role, session_factory
):
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    a_kb = await _seed_kb(session_factory, org_a, name=f"a-{uuid.uuid4()}")
    b_kb = await _seed_kb(session_factory, org_b, name=f"b-{uuid.uuid4()}")
    a_entry = await _seed_wiki_entry(session_factory, a_kb)
    b_entry = await _seed_wiki_entry(session_factory, b_kb)

    async with session_factory() as db:
        async with as_tenant(db, org_a):
            ids = {
                r.id
                for r in (
                    await db.execute(text("SELECT id FROM kb_wiki_entry"))
                ).all()
            }
    assert a_entry in ids
    assert b_entry not in ids


# ---------------------------------------------------------------------------
# Isolation on kb_chunk (grandchild via wiki_entry)
# ---------------------------------------------------------------------------


async def test_kb_chunk_isolated_via_grandparent_kb(
    kb_app_role, session_factory
):
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    a_kb = await _seed_kb(session_factory, org_a, name=f"a-{uuid.uuid4()}")
    b_kb = await _seed_kb(session_factory, org_b, name=f"b-{uuid.uuid4()}")
    a_entry = await _seed_wiki_entry(session_factory, a_kb)
    b_entry = await _seed_wiki_entry(session_factory, b_kb)

    a_chunk = uuid.uuid4()
    b_chunk = uuid.uuid4()
    async with session_factory() as db:
        for cid, eid in [(a_chunk, a_entry), (b_chunk, b_entry)]:
            await db.execute(
                text(
                    "INSERT INTO kb_chunk "
                    "(id, wiki_entry_id, chunk_index, content) "
                    "VALUES (:id, :eid, 0, 'x')"
                ),
                {"id": cid, "eid": eid},
            )
        await db.commit()

    async with session_factory() as db:
        async with as_tenant(db, org_a):
            ids = {
                r.id
                for r in (
                    await db.execute(text("SELECT id FROM kb_chunk"))
                ).all()
            }
    assert a_chunk in ids
    assert b_chunk not in ids


async def test_app_role_cannot_insert_chunk_under_other_orgs_wiki_entry(
    kb_app_role, session_factory
):
    """The two-hop check (chunk -> wiki_entry -> kb) walked by RLS
    rejects writes through another tenant's parent chain.
    """
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    b_kb = await _seed_kb(session_factory, org_b, name=f"b-{uuid.uuid4()}")
    b_entry = await _seed_wiki_entry(session_factory, b_kb)

    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as db:
            async with as_tenant(db, org_a):
                await db.execute(
                    text(
                        "INSERT INTO kb_chunk "
                        "(id, wiki_entry_id, chunk_index, content) "
                        "VALUES (:id, :eid, 0, 'pwned')"
                    ),
                    {"id": uuid.uuid4(), "eid": b_entry},
                )
                await db.commit()
    assert "row-level security" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Read with platform: app role can SELECT platform rows even though it
# cannot WRITE them.
# ---------------------------------------------------------------------------


async def test_app_role_can_read_platform_chunks_but_not_insert(
    kb_app_role, session_factory
):
    """End-to-end: as the app role with org A's GUC,
    1. platform-KB chunks are visible (read_with_platform policy)
    2. inserting a chunk under a platform wiki entry is rejected
       (strict_own policy)
    """
    plat_kb = await _seed_kb(session_factory, None, name=f"plat-{uuid.uuid4()}")
    plat_entry = await _seed_wiki_entry(session_factory, plat_kb)
    chunk_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb_chunk "
                "(id, wiki_entry_id, chunk_index, content) "
                "VALUES (:id, :eid, 0, 'platform content')"
            ),
            {"id": chunk_id, "eid": plat_entry},
        )
        await db.commit()

    org = await create_org(session_factory)

    async with session_factory() as db:
        async with as_tenant(db, org):
            # 1. SELECT works.
            row = (
                await db.execute(
                    text("SELECT content FROM kb_chunk WHERE id = :id"),
                    {"id": chunk_id},
                )
            ).first()
            assert row is not None
            assert row.content == "platform content"

    # 2. INSERT into platform's wiki entry is rejected.
    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as db:
            async with as_tenant(db, org):
                await db.execute(
                    text(
                        "INSERT INTO kb_chunk "
                        "(id, wiki_entry_id, chunk_index, content) "
                        "VALUES (:id, :eid, 1, 'pwned')"
                    ),
                    {"id": uuid.uuid4(), "eid": plat_entry},
                )
                await db.commit()
    assert "row-level security" in str(exc_info.value).lower()
