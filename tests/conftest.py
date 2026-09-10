"""Shared fixtures for the Surogates test suite."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CheckConstraint

from surogates.db.models import Base
from surogates.tenant.context import TenantContext


# ---------------------------------------------------------------------------
# Ensure JWT secret is available for all tests that need it.
# ---------------------------------------------------------------------------
os.environ.setdefault("SUROGATES_JWT_SECRET", "test-secret-key-for-unit-tests")

# ---------------------------------------------------------------------------
# Run document parsing in-thread for tests so that monkeypatched
# ``_load_liteparse`` is visible.  Production defaults to the subprocess
# pool (see ``surogates.tools.builtin.file_ops``).
# ---------------------------------------------------------------------------
os.environ.setdefault("SUROGATES_DOCUMENT_PARSE_USE_SUBPROCESS", "0")


# ---------------------------------------------------------------------------
# Running the surogates schema on in-memory SQLite
#
# Most of the metadata is Postgres-only: 26 columns are bare ``JSONB``, many
# primary keys are bare ``postgresql.UUID``, and four tables carry a CHECK
# constraint written with the Postgres-only ``::int`` cast, and BigInteger
# identities do not autoincrement there.  The four hooks
# below teach SQLite to render each of those, and ``sqlite_tables`` builds
# only the tables a test names, because whole-metadata creation drags in
# everything whether the test needs it or not.
#
# These hooks are process-global, but they only ever ADD capability: before
# them, compiling any of these constructs against SQLite raised outright, so
# no previously-passing test can change behaviour.
# ---------------------------------------------------------------------------

@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(PGUUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@compiles(BigInteger, "sqlite")
def _bigint_on_sqlite(type_, compiler, **kw):
    # SQLite auto-assigns a rowid only for INTEGER PRIMARY KEY — a BIGINT one
    # stays NULL and the insert fails. events.id and inbox_items.id are both
    # BigInteger identities, so without this the escalation path cannot be
    # tested at all. INTEGER is 64-bit in SQLite, so nothing is lost.
    return "INTEGER"


@compiles(CheckConstraint, "sqlite")
def _check_on_sqlite(element, compiler, **kw):
    # SQLite has no ``::int`` cast and does not need one — a boolean there is
    # already 0 or 1, so dropping the cast preserves the constraint's meaning.
    return compiler.visit_check_constraint(element, **kw).replace("::int", "")


def sqlite_tables(*names: str) -> list:
    """The named tables, for ``create_all(tables=...)``.

    A name that matches nothing is ignored, so callers may list tables
    optimistically.
    """
    wanted = set(names)
    return [t for name, t in Base.metadata.tables.items() if name in wanted]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def sf():
    """Session factory over an in-memory SQLite database with the program tables.

    StaticPool keeps every session on the one connection that owns the
    database; without it each session would see a fresh, empty schema.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
    )
    tables = sqlite_tables(
        "program_schedules", "program_occurrences", "program_invitations",
        "inbox_items", "events", "sessions", "orgs", "users",
        "channel_identities", "delivery_outbox",
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _make_program_invitation(
    sf, *, delivery_state: str, response_state: str, **extra,
):
    """One occurrence carrying one invitation, for the delivery/sweep tests."""
    from datetime import datetime, timezone

    from surogates.db.models import ProgramInvitationRow, ProgramOccurrenceRow

    now = datetime.now(timezone.utc)
    async with sf() as db:
        occ = ProgramOccurrenceRow(
            org_id=uuid4(),
            program_id=uuid4(),
            agent_id="a1",
            scheduled_for=now,
            skill_ref="post-op",
            template_name="daily",
            template_language="en_US",
            status="fired",
        )
        db.add(occ)
        await db.flush()
        row = ProgramInvitationRow(
            occurrence_id=occ.id,
            program_id=occ.program_id,
            org_id=occ.org_id,
            agent_id="a1",
            user_id=uuid4(),
            platform=extra.pop("platform", "whatsapp"),
            platform_user_id=extra.pop("platform_user_id", "40746148303"),
            delivery_state=delivery_state,
            response_state=response_state,
            escalation_state="none",
            **extra,
        )
        db.add(row)
        await db.commit()
        return row


async def _reload_program_invitation(sf, invitation_id):
    from surogates.db.models import ProgramInvitationRow

    async with sf() as db:
        return await db.get(ProgramInvitationRow, invitation_id)


@pytest.fixture
def make_invitation(sf):
    """``await make_invitation(delivery_state=..., response_state=..., ...)``."""

    async def _make(**kwargs):
        return await _make_program_invitation(sf, **kwargs)

    return _make


@pytest.fixture
def reload_invitation(sf):
    """``await reload_invitation(invitation_id)`` — re-read from the database."""

    async def _reload(invitation_id):
        return await _reload_program_invitation(sf, invitation_id)

    return _reload


@pytest.fixture()
def tenant_context(tmp_path: Path) -> TenantContext:
    """A default TenantContext for functional tests."""
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={
            "agent_name": "TestAgent",
            "personality": "You are a helpful test assistant.",
            "default_model": "gpt-4o",
        },
        user_preferences={"theme": "dark", "language": "en"},
        permissions=frozenset({"read", "write", "admin"}),
        asset_root=str(tmp_path),
    )


@pytest.fixture()
def tmp_asset_root(tmp_path: Path) -> Path:
    """A temp directory structured as a tenant asset root.

    Layout::

        {tmp}/ORG_ID/shared/{memory,skills,mcp,tools}/
        {tmp}/ORG_ID/users/USER_ID/{memory,skills,mcp,tools}/
    """
    org_id = "00000000-0000-0000-0000-000000000001"
    user_id = "00000000-0000-0000-0000-000000000002"

    subdirs = ("memory", "skills", "mcp", "tools")
    shared_root = tmp_path / org_id / "shared"
    user_root = tmp_path / org_id / "users" / user_id

    for subdir in subdirs:
        (shared_root / subdir).mkdir(parents=True, exist_ok=True)
        (user_root / subdir).mkdir(parents=True, exist_ok=True)

    return tmp_path
