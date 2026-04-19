"""Shared fixtures for integration tests using testcontainers.

Spins up real PostgreSQL 16 and Redis 7 containers once per test session
and provides async engine, session factory, and store fixtures.
"""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

import bcrypt as _bcrypt

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import Base
from surogates.session.store import SessionStore
from surogates.tenant.auth.service_account import _reset_caches as _reset_sa_caches

# Ensure JWT secret is set for all integration tests.
os.environ.setdefault("SUROGATES_JWT_SECRET", "integration-test-secret-key-1234")


# ---------------------------------------------------------------------------
# Global hygiene
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _sa_cache_reset():
    """Clear the in-process service-account auth caches between tests.

    The caches are module-level singletons, so one test's cached
    resolution would otherwise bleed into the next and mask
    correctness bugs (e.g. a revoked SA still accepted because a
    prior test populated the cache).  Reset both before and after
    so tests that deliberately warm the cache can do so without
    depending on earlier test ordering.
    """
    _reset_sa_caches()
    yield
    _reset_sa_caches()


# ---------------------------------------------------------------------------
# Containers -- started once, shared across all tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def postgres_container():
    """Spin up a PostgreSQL 16 container for the test session."""
    with PostgresContainer("postgres:16", driver="asyncpg") as pg:
        yield pg


@pytest.fixture(scope="session")
def redis_container():
    """Spin up a Redis 7 container for the test session."""
    with RedisContainer("redis:7") as r:
        yield r


@pytest.fixture(scope="session")
def pg_url(postgres_container):
    """Async PostgreSQL connection URL."""
    url = postgres_container.get_connection_url()
    # testcontainers may give psycopg2 URL; ensure asyncpg driver
    if "psycopg2" in url:
        url = url.replace("psycopg2", "asyncpg")
    if "postgresql://" in url and "postgresql+asyncpg://" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://")
    return url


@pytest.fixture(scope="session")
def redis_url(redis_container):
    """Build a Redis connection URL from the test container."""
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


# ---------------------------------------------------------------------------
# Database engine and table creation (session-scoped)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(pg_url):
    """Create async engine and create all tables once per session."""
    eng = create_async_engine(
        pg_url,
        pool_size=5,
        connect_args={"statement_cache_size": 0},
    )

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await apply_observability_ddl(conn)

    yield eng

    await eng.dispose()


# ---------------------------------------------------------------------------
# Per-test fixtures (use loop_scope="session" so they run on the same loop
# as the session-scoped engine)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(loop_scope="session")
async def session_factory(engine):
    """Return an async_sessionmaker bound to the test engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def session_store(session_factory):
    """Return a SessionStore backed by the test database."""
    return SessionStore(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def redis_client(redis_url):
    """Async Redis client, closed after each test."""
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url, decode_responses=False)
    yield client
    await client.aclose()


# ---------------------------------------------------------------------------
# Helpers -- create org + user to satisfy FK constraints
# ---------------------------------------------------------------------------

async def create_org(session_factory: async_sessionmaker, org_id: UUID | None = None) -> UUID:
    """Insert an org row and return its id."""
    oid = org_id or uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, :name)"),
            {"id": oid, "name": f"test-org-{oid}"},
        )
        await db.commit()
    return oid


async def create_user(
    session_factory: async_sessionmaker,
    org_id: UUID,
    user_id: UUID | None = None,
    email: str | None = None,
    password: str | None = None,
) -> UUID:
    """Insert a user row and return its id."""
    uid = user_id or uuid.uuid4()
    email = email or f"user-{uid}@test.com"
    password_hash = (
        _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=4)).decode()
        if password
        else None
    )
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO users (id, org_id, email, display_name, password_hash) "
                "VALUES (:id, :org_id, :email, :display_name, :password_hash)"
            ),
            {
                "id": uid,
                "org_id": org_id,
                "email": email,
                "display_name": f"Test User {uid}",
                "password_hash": password_hash,
            },
        )
        await db.commit()
    return uid


async def issue_service_account_token(
    session_factory,
    org_id: UUID,
    name: str = "pipeline",
):
    """Create a service account and return its raw bearer token.

    Returns the :class:`IssuedServiceAccount` record — callers need
    ``.token`` for the ``Authorization: Bearer`` header and ``.id`` as
    the expected service-account id in assertions.
    """
    from surogates.tenant.auth.service_account import ServiceAccountStore

    return await ServiceAccountStore(session_factory).create(
        org_id=org_id, name=name,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def org_and_user(session_factory):
    """Create a fresh org + user pair, returning (org_id, user_id)."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    return org_id, user_id
