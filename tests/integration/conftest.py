"""Shared fixtures for integration tests using testcontainers.

Spins up real PostgreSQL 16 and Redis 7 containers once per test session
and provides async engine, session factory, and store fixtures.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
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

from .inbox_e2e_helpers import build_inbox_test_app

# Ensure JWT secret is set for all integration tests.
os.environ.setdefault("SUROGATES_JWT_SECRET", "integration-test-secret-key-1234")
# ``tenant_assets_root`` defaults to ``/data/tenant-assets``, an absolute
# container path. Every fixture that builds a LocalBackend failed on the
# filesystem before reaching an assertion, so those suites only ever ran
# inside the image. Set once here rather than per fixture — the copy-paste
# version reached 4 of the 9 modules that call create_backend.
os.environ.setdefault(
    "SUROGATES_STORAGE_BASE_PATH",
    tempfile.mkdtemp(prefix="surogates-tenant-assets-"),
)


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


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _flush_rate_limit_keys(redis_client):
    """Clear the API rate-limit Redis keys between integration tests.

    The production rate limiter derives its Redis key from the first 32
    characters of the JWT, which for HS256 tokens is always the same
    algorithm/typ header.  Combined with the session-scoped Redis
    container this bucket is effectively shared across every test in
    the module, and once the suite exceeds 120 requests/minute every
    subsequent test fails with 429.

    Flushing ``surogates:rate:*`` before each test gives each one a
    fresh budget without touching other Redis state.  Reuses the
    session-scoped ``redis_client`` so we don't open a fresh TCP
    connection per test.
    """
    try:
        keys = [k async for k in redis_client.scan_iter(match="surogates:rate:*")]
        if keys:
            await redis_client.unlink(*keys)
    except Exception:
        pass
    yield


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


@pytest_asyncio.fixture(loop_scope="session")
async def inbox_app(session_factory, redis_client, pg_url, redis_url):
    """FastAPI app configured for inbox end-to-end route tests."""
    return build_inbox_test_app(session_factory, redis_client, pg_url, redis_url)


@pytest_asyncio.fixture(loop_scope="session")
async def inbox_client(inbox_app):
    """HTTP client for inbox end-to-end route tests."""
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app),
        base_url="http://test",
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Calls stopped at a wait of the test's choosing
# ---------------------------------------------------------------------------

def _stop() -> None:
    asyncio.current_task().cancel()


class _StoppedAt:
    """Awaits a call, asking for its task's cancellation as the call reaches one of its waits."""

    def __init__(self, call, at: int, stop) -> None:
        self._call, self._at, self._stop = call, at, stop

    def __await__(self):
        call = self._call.__await__()
        waits, send, throw = 0, None, None
        while True:
            try:
                waited = call.send(send) if throw is None else call.throw(throw)
            except StopIteration as ended:
                return ended.value
            if waits == self._at:
                # Asked for by the task itself as it suspends: the stop lands at this
                # wait and no other, as one from outside does when it comes during it.
                self._stop()
            waits += 1
            send = throw = None
            try:
                send = yield waited
            except BaseException as raised:
                throw = raised


class Stopping:
    """An engine of a test's own, set as the worker's is, and calls on it stopped at one of their waits.

    Its connections carry a name of their own: those a stopped call still
    holds are then the test's to count, and to end.
    """

    def __init__(self, pg_url: str, session_factory: async_sessionmaker) -> None:
        self._name = f"stopping-{uuid.uuid4()}"
        self._others = session_factory
        self.engine = create_async_engine(pg_url, connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "server_settings": {"application_name": self._name},
        })
        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def stop_at(self, at: int, call, *, stop=_stop, within: float = 10.0) -> bool:
        """Run *call* in a task of its own, stopped at its wait number *at*, counted from 0.

        Returns once the task ended with its connections given back: True when
        it was stopped, False when the call ended before that wait.  A sweep of
        *at* from 0 stops a call at each of its waits in turn.  *stop* is what
        stops it, run in the task itself: its cancellation, unless given.
        """
        async def stopped():
            return await _StoppedAt(call, at, stop)

        # A connection in the pool first: the waits counted are the call's own, not those of connecting.
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        task = asyncio.create_task(stopped())
        try:
            ended, _ = await asyncio.wait({task}, timeout=within)
            assert ended, f"stopped at its wait {at}, the call did not end"
        finally:
            if not task.done():
                # Not left behind: a task that never ends keeps the run's loop from closing.  Stopped
                # again, and its connections ended under it, it has nothing left to wait for.
                task.cancel()
                async with self._others() as db:
                    await db.execute(
                        text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = :name"),
                        {"name": self._name},
                    )
                await asyncio.wait({task}, timeout=within)
        assert self.engine.pool.checkedout() == 0, "the stopped call kept a connection"
        if task.cancelled():
            return True
        task.result()
        return False


@pytest_asyncio.fixture(loop_scope="session")
async def stopping(pg_url, session_factory):
    """Calls on the test database stopped at one of their waits (see Stopping)."""
    stopping = Stopping(pg_url, session_factory)
    yield stopping
    await stopping.engine.dispose()


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


@pytest_asyncio.fixture(loop_scope="session")
async def seeded_org_and_session(session_factory):
    """Insert org + user + session + mission rows; return their ids.

    Shared by the arbor research-mission tests, which need a session
    (FK target for ``research_runs``) and a mission (FK target for the
    ``research_runs.mission_id`` sidecar link).
    """
    from surogates.db.models import Mission, Session

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(Session(
            id=session_id, org_id=org_id, user_id=user_id,
            agent_id="agent-x", config={},
        ))
        await db.commit()
        mission = Mission(
            org_id=org_id, user_id=user_id, session_id=session_id,
            agent_id="agent-x", description="d", rubric="r",
        )
        db.add(mission)
        await db.commit()
        await db.refresh(mission)
        mission_id = mission.id
    return org_id, mission_id, session_id


async def leave_mid_stream(app, url: str, headers: dict[str, str], *, after: bytes) -> None:
    """GET the stream at *url* from *app*, and leave it once *after* is sent,
    while the stream waits for its next change.

    Driven over ASGI: the test client reads a response whole, so it never
    leaves one mid-stream.
    """
    import asyncio

    path, _, query = url.partition("?")
    left, requested = asyncio.Event(), asyncio.Event()

    async def receive():
        if not requested.is_set():
            requested.set()
            return {"type": "http.request", "body": b"", "more_body": False}
        await left.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if after in message.get("body", b""):
            asyncio.get_running_loop().call_later(0.2, left.set)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": ("127.0.0.1", 1), "server": ("test", 80),
    }
    async with asyncio.timeout(10):
        await app(scope, receive, send)
    assert left.is_set(), "the stream ended before the client left"
