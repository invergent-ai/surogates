"""A sandbox a turn end left up is reaped once its session and the agents below it are at rest.

On 2026-11-20 four sandboxes outlived their sessions; three of them were the
case below.  A root's turn ended while a background agent it started ran in
its sandbox, so that turn end left the sandbox up.  The agent's report woke
the root on another worker, whose turn end swept the root's containers but
spared the first worker's, a live worker's.  The agent's own container, on
its worker, was never swept either.  A runaway command in it ran on for an hour.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from surogates.sandbox.base import SandboxSpec, SandboxStatus
from surogates.sandbox.pool import SandboxPool


class FakeBackend:
    def __init__(self) -> None:
        self.live: set[str] = set()
        self.destroyed: list[str] = []
        self.swept: list[str] = []
        self.gate: asyncio.Event | None = None

    async def provision(self, spec: SandboxSpec) -> str:
        sid = f"sbx-{len(self.live) + len(self.destroyed) + 1}"
        self.live.add(sid)
        return sid

    async def execute(self, sandbox_id: str, name: str, input: str, *, timeout: float | None = None) -> str:
        if self.gate is not None:
            await self.gate.wait()
        return "{}"

    async def status(self, sandbox_id: str) -> SandboxStatus:
        return SandboxStatus.RUNNING if sandbox_id in self.live else SandboxStatus.TERMINATED

    async def destroy(self, sandbox_id: str) -> None:
        self.live.discard(sandbox_id)
        self.destroyed.append(sandbox_id)

    async def destroy_for_session(self, session_id: str) -> None:
        self.swept.append(session_id)


def rest(*keys: str):
    async def at_rest(key: str) -> bool:
        return key in keys
    return at_rest


ROOT = str(uuid.uuid4())


async def test_the_sandbox_a_turn_end_left_for_an_agent_goes_once_all_are_at_rest():
    backend = FakeBackend()
    pool = SandboxPool(backend)
    sid = await pool.ensure(ROOT, SandboxSpec())
    # The agent below still runs: its sandbox stays.
    assert await pool.reap_at_rest(rest(), idle_seconds=0) == []
    assert pool.sandbox_of(ROOT) == sid
    # All at rest: it goes, and the backend's sweep of the session runs too (the containers of gone workers).
    assert await pool.reap_at_rest(rest(ROOT), idle_seconds=0) == [ROOT]
    assert pool.sandbox_of(ROOT) is None
    assert backend.destroyed == [sid] and backend.swept == [ROOT]
    # A later turn gets a fresh one.
    assert await pool.ensure(ROOT, SandboxSpec()) != sid


async def test_a_sandbox_used_within_the_idle_time_stays():
    backend = FakeBackend()
    pool = SandboxPool(backend)
    await pool.ensure(ROOT, SandboxSpec())
    assert await pool.reap_at_rest(rest(ROOT), idle_seconds=60) == []
    assert backend.destroyed == []


async def test_a_sandbox_with_a_call_in_flight_stays():
    backend = FakeBackend()
    backend.gate = asyncio.Event()
    pool = SandboxPool(backend)
    sid = await pool.ensure(ROOT, SandboxSpec())
    call = asyncio.ensure_future(pool.execute(ROOT, "terminal", "{}"))
    await asyncio.sleep(0)
    assert await pool.reap_at_rest(rest(ROOT), idle_seconds=0) == []
    backend.gate.set()
    await call
    assert await pool.reap_at_rest(rest(ROOT), idle_seconds=0) == [ROOT]
    assert backend.destroyed == [sid]


async def test_a_threads_pod_over_its_copy_is_never_reaped():
    """Its keep or landing decides what becomes of the work in its copy."""
    backend = FakeBackend()
    pool = SandboxPool(backend)
    await pool.ensure(ROOT, SandboxSpec(env={"PROJECT_DIR": "/project"}))
    assert await pool.reap_at_rest(rest(ROOT), idle_seconds=0) == []
    assert backend.destroyed == []


async def test_a_session_whose_rest_cannot_be_told_keeps_its_sandbox(caplog):
    async def unsure(key: str) -> bool:
        raise RuntimeError("database away")

    backend = FakeBackend()
    pool = SandboxPool(backend)
    await pool.ensure(ROOT, SandboxSpec())
    assert await pool.reap_at_rest(unsure, idle_seconds=0) == []
    assert backend.destroyed == []
    assert "sandboxes stay" in caplog.text


async def test_a_turn_that_takes_the_sandbox_again_meanwhile_keeps_it():
    """At rest when asked, then a new turn asks for its sandbox before the reap takes it: the turn keeps it."""
    backend = FakeBackend()
    pool = SandboxPool(backend)
    sid = await pool.ensure(ROOT, SandboxSpec())
    pool._used[ROOT] -= 600  # unused for ten minutes
    asked = []

    async def at_rest_then_a_turn(key: str) -> bool:
        asked.append(key)
        await pool.ensure(ROOT, SandboxSpec())
        return True

    assert await pool.reap_at_rest(at_rest_then_a_turn, idle_seconds=300) == []
    assert asked == [ROOT]
    assert pool.sandbox_of(ROOT) == sid and backend.destroyed == []


async def test_a_turn_that_asks_during_the_destroy_waits_and_gets_a_fresh_sandbox_no_sweep_takes():
    """The destroy, and the backend's sweep of the session, run under the session's lock."""
    gate = asyncio.Event()

    class SlowDestroy(FakeBackend):
        async def destroy(self, sandbox_id):
            await gate.wait()
            await super().destroy(sandbox_id)

        async def destroy_for_session(self, session_id):
            await super().destroy_for_session(session_id)
            self.live.clear()  # a sweep takes every container of the session this worker holds

    backend = SlowDestroy()
    pool = SandboxPool(backend)
    old = await pool.ensure(ROOT, SandboxSpec())
    reap = asyncio.ensure_future(pool.reap_at_rest(rest(ROOT), idle_seconds=0))
    await asyncio.sleep(0.01)
    turn = asyncio.ensure_future(pool.ensure(ROOT, SandboxSpec()))
    await asyncio.sleep(0.01)
    assert not turn.done()  # waits for the reap
    gate.set()
    assert await reap == [ROOT]
    fresh = await turn
    assert fresh != old and fresh in backend.live


async def test_the_reaper_runs_on_its_own_and_stops_with_the_pool():
    backend = FakeBackend()
    pool = SandboxPool(backend)
    sid = await pool.ensure(ROOT, SandboxSpec())
    pool.start_reaping(rest(ROOT), every=0.01, idle_seconds=0.01)
    for _ in range(200):
        if backend.destroyed:
            break
        await asyncio.sleep(0.01)
    assert backend.destroyed == [sid]
    reaper = pool._reaper
    await pool.destroy_all()
    assert reaper.done() and pool._reaper is None


async def test_an_idle_time_or_a_round_of_zero_turns_the_reaper_off():
    pool = SandboxPool(FakeBackend())
    pool.start_reaping(rest(ROOT), every=0.01, idle_seconds=0)
    pool.start_reaping(rest(ROOT), every=0, idle_seconds=300)
    assert pool._reaper is None


# --- who is at rest ----------------------------------------------------------------------------------------------


@pytest.fixture
async def sessions_db():
    from surogates.db.models import Base
    from tests.conftest import sqlite_tables

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=sqlite_tables("sessions", "orgs", "users", "agent_users"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _session(factory, *, status: str, parent: uuid.UUID | None = None, root: uuid.UUID | None = None) -> uuid.UUID:
    from surogates.db.models import Session as SessionRow

    sid = uuid.uuid4()
    async with factory() as db:
        db.add(SessionRow(
            id=sid, org_id=uuid.uuid4(), agent_id="a", status=status, parent_id=parent,
            config={"sandbox_root_session_id": str(root)} if root else {},
        ))
        await db.commit()
    return sid


async def _set(factory, sid: uuid.UUID, status: str) -> None:
    from surogates.db.models import Session as SessionRow

    async with factory() as db:
        (await db.get(SessionRow, sid)).status = status
        await db.commit()


async def test_at_rest_needs_the_session_and_every_agent_below_it_at_rest(sessions_db):
    from surogates.harness.subagents import sandbox_key_at_rest

    root = await _session(sessions_db, status="completed")
    agent = await _session(sessions_db, status="active", parent=root, root=root)
    # The root's turn ended while its background agent still runs: not at rest.
    assert await sandbox_key_at_rest(sessions_db, str(root)) is False
    await _set(sessions_db, agent, "completed")
    # The agent's report woke the root: not at rest while that turn runs.
    await _set(sessions_db, root, "active")
    assert await sandbox_key_at_rest(sessions_db, str(root)) is False
    await _set(sessions_db, root, "completed")
    assert await sandbox_key_at_rest(sessions_db, str(root)) is True
    # A stopped or paused session's own sandbox went with its stop: its leftovers elsewhere are at rest too.
    await _set(sessions_db, root, "paused")
    assert await sandbox_key_at_rest(sessions_db, str(root)) is True
    # An agent stopped by stop_agent is paused for good: at rest.
    await _set(sessions_db, agent, "paused")
    await _set(sessions_db, root, "completed")
    assert await sandbox_key_at_rest(sessions_db, str(root)) is True


async def test_an_agent_below_an_agent_counts_down_the_tree(sessions_db):
    from surogates.harness.subagents import sandbox_key_at_rest

    root = await _session(sessions_db, status="completed")
    child = await _session(sessions_db, status="completed", parent=root, root=root)
    await _session(sessions_db, status="active", parent=child, root=root)
    assert await sandbox_key_at_rest(sessions_db, str(root)) is False


async def test_a_thread_under_a_master_works_in_a_sandbox_of_its_own(sessions_db):
    """A project's thread carries its own stamp: running, it does not hold its master's sandbox."""
    from surogates.harness.subagents import sandbox_key_at_rest

    master = await _session(sessions_db, status="completed")
    thread = uuid.uuid4()
    from surogates.db.models import Session as SessionRow

    async with sessions_db() as db:
        db.add(SessionRow(id=thread, org_id=uuid.uuid4(), agent_id="a", status="active", parent_id=master,
                          config={"sandbox_root_session_id": str(thread)}))
        await db.commit()
    assert await sandbox_key_at_rest(sessions_db, str(master)) is True
    assert await sandbox_key_at_rest(sessions_db, str(thread)) is False


async def test_an_agent_known_only_by_its_parent_counts_too(sessions_db):
    from surogates.harness.subagents import sandbox_key_at_rest

    root = await _session(sessions_db, status="failed")
    await _session(sessions_db, status="active", parent=root)
    assert await sandbox_key_at_rest(sessions_db, str(root)) is False


async def test_a_gone_session_is_at_rest_and_a_key_that_is_no_session_is_not(sessions_db):
    from surogates.harness.subagents import sandbox_key_at_rest

    assert await sandbox_key_at_rest(sessions_db, str(uuid.uuid4())) is True
    assert await sandbox_key_at_rest(sessions_db, "device:abc") is False


async def test_a_sandbox_the_watch_holds_is_not_at_rest(sessions_db, monkeypatch):
    from surogates.harness import background_watch as bw
    from surogates.harness.subagents import sandbox_key_at_rest

    root = await _session(sessions_db, status="completed")
    monkeypatch.setattr(bw.background_watch, "holds", lambda owner: owner == str(root))
    assert await sandbox_key_at_rest(sessions_db, str(root)) is False


async def test_a_sandbox_whose_destroy_failed_is_asked_for_again_next_round():
    """Kubernetes' destroy is to be asked again after a failed delete, and has no label sweep to go by."""
    class Flaky(FakeBackend):
        fail = True

        async def destroy(self, sandbox_id):
            if self.fail:
                raise RuntimeError("the cluster did not answer")
            await super().destroy(sandbox_id)

    backend = Flaky()
    pool = SandboxPool(backend)
    sid = await pool.ensure(ROOT, SandboxSpec())
    assert await pool.reap_at_rest(rest(ROOT), idle_seconds=0) == []
    assert pool.sandbox_of(ROOT) == sid
    backend.fail = False
    assert await pool.reap_at_rest(rest(ROOT), idle_seconds=0) == [ROOT]
    assert backend.destroyed == [sid]


async def test_a_reaper_stopped_mid_destroy_waits_for_the_destroy():
    gate = asyncio.Event()

    class Slow(FakeBackend):
        async def destroy(self, sandbox_id):
            await gate.wait()
            await super().destroy(sandbox_id)

    backend = Slow()
    pool = SandboxPool(backend)
    sid = await pool.ensure(ROOT, SandboxSpec())
    pool.start_reaping(rest(ROOT), every=0.01, idle_seconds=0.01)
    await asyncio.sleep(0.1)
    stopping = asyncio.ensure_future(pool.stop_reaping())
    await asyncio.sleep(0.05)
    gate.set()
    await stopping
    assert backend.destroyed == [sid]
async def test_a_sandbox_in_the_sessions_own_image_is_not_this_pools_to_reap():
    """In its own image the container is the session's workspace, which another worker may be using: the backend's
    reaper removes it, by the container's own clock."""
    backend = FakeBackend()
    pool = SandboxPool(backend)
    image = await pool.ensure(ROOT, SandboxSpec(session_image="registry.example/tasks/x:1"))
    assert await pool.reap_at_rest(rest(ROOT), idle_seconds=0) == []
    assert backend.destroyed == [] and pool.sandbox_of(ROOT) == image
