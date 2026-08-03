"""A task whose spawn keeps failing must stop being re-claimed.

``_enqueue_ready_tasks`` claims a ready task with
``status='running', attempt_count + 1``, then spawns the child session.
Every failure except ``UnknownAgentDefError`` called ``_rollback_claim``,
which restored ``status='ready'`` **and decremented ``attempt_count`` back**
-- byte-identical to the pre-claim row.

That made the ceiling unreachable. ``attempt_count >= max_attempts`` is
checked in ``_finalize_ended_attempts``, which only inspects tasks in
``running`` joined to an ended Session; a rolled-back task is ``ready`` with
no ``current_session_id``, so it is never finalised and never fails. The
``failed_this_tick`` guard is explicitly per-tick. The tick runs every 5s.

The trigger is permanent, not transient: ``create_child_session`` raises
``ValueError`` when the parent's config lacks ``storage_bucket`` /
``storage_key_prefix`` / ``workspace_path``, and that is a property of the
parent row -- it never self-heals.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from surogates.db.models import Session as ORMSession, Task
from surogates.tasks.dispatcher import _enqueue_ready_tasks

from tests.integration.conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def broken_parent(session_factory, org_id: uuid.UUID) -> ORMSession:
    """A parent missing ``storage_key_prefix``.

    Not hypothetical: ``api/routes/website.py`` creates sessions with
    ``storage_bucket`` and ``workspace_path`` only.
    """
    pid = uuid.uuid4()
    async with session_factory() as db:
        s = ORMSession(
            id=pid, org_id=org_id, agent_id="orchestrator",
            channel="website", status="active",
            config={
                "storage_bucket": "test-bucket",
                "workspace_path": f"/workspace/test/{pid}",
            },
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


def _tenant_for_task(task):
    return MagicMock(org_id=task.org_id)


async def _tick(session_factory, session_store) -> None:
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    await _enqueue_ready_tasks(
        session_factory=session_factory,
        redis=redis,
        session_store=session_store,
        tenant_for_task=_tenant_for_task,
    )


async def _add_ready_task(session_factory, org_id, parent, **kw) -> uuid.UUID:
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=parent.id,
            goal="do the thing", status="ready", **kw,
        )
        db.add(t)
        await db.commit()
        return t.id


async def test_a_permanently_broken_spawn_stops_being_retried(
    session_factory, session_store, org_id, broken_parent,
):
    """The whole bug: ticks 1..N must not all re-claim the same dead row."""
    tid = await _add_ready_task(
        session_factory, org_id, broken_parent, max_attempts=3,
    )

    for _ in range(5):
        await _tick(session_factory, session_store)

    async with session_factory() as db:
        task = await db.get(Task, tid)

    assert task.status != "ready", (
        "a task whose spawn can never succeed must leave the ready pool; "
        "left 'ready' it is re-claimed every 5s forever"
    )
    assert task.status in {"blocked", "failed"}
    assert task.current_session_id is None


async def test_a_permanent_config_error_is_blocked_on_the_first_tick(
    session_factory, session_store, org_id, broken_parent,
):
    """A missing workspace field is a property of the parent row.

    It cannot self-heal, so retrying it even once is wasted work -- the
    existing ``UnknownAgentDefError`` carve-out treats its own permanent
    error exactly this way.
    """
    tid = await _add_ready_task(
        session_factory, org_id, broken_parent, max_attempts=5,
    )

    await _tick(session_factory, session_store)

    async with session_factory() as db:
        task = await db.get(Task, tid)
    assert task.status == "blocked", (
        "a permanent config error must not wait for a retry ceiling"
    )


async def test_an_unclassified_failure_retries_but_is_bounded(
    session_factory, session_store, org_id: uuid.UUID, monkeypatch,
):
    """An error we cannot classify is worth retrying -- but not forever.

    This is the catch-all path, which had no bound at all.
    """
    pid = uuid.uuid4()
    async with session_factory() as db:
        parent = ORMSession(
            id=pid, org_id=org_id, agent_id="orchestrator",
            channel="web", status="active",
            config={
                "storage_bucket": "test-bucket",
                "storage_key_prefix": "",
                "workspace_path": f"/workspace/test/{pid}",
                "supports_vision": False,
            },
        )
        db.add(parent)
        await db.commit()
        await db.refresh(parent)

    tid = await _add_ready_task(
        session_factory, org_id, parent, max_attempts=2,
    )

    async def boom(*_a, **_kw):
        raise RuntimeError("transient-looking but never recovers")

    # The dispatcher imports this inside the function, so patch the source.
    monkeypatch.setattr(
        "surogates.tasks.spawn._create_session_for_task", boom,
    )

    # First tick: retried, still eligible.
    await _tick(session_factory, session_store)
    async with session_factory() as db:
        assert (await db.get(Task, tid)).status == "ready"

    # Ceiling reached on the second.
    await _tick(session_factory, session_store)
    async with session_factory() as db:
        task = await db.get(Task, tid)
    assert task.status != "ready", (
        "an unclassified failure must still stop being re-claimed once the "
        "task has burned max_attempts"
    )


async def test_the_block_reason_names_the_cause(
    session_factory, session_store, org_id, broken_parent,
):
    """An operator must be able to see why without reading worker logs."""
    tid = await _add_ready_task(
        session_factory, org_id, broken_parent, max_attempts=1,
    )

    await _tick(session_factory, session_store)

    async with session_factory() as db:
        task = await db.get(Task, tid)
    assert task.status == "blocked"
    assert task.blocked_reason
    assert "storage_key_prefix" in task.blocked_reason


async def test_a_healthy_task_is_unaffected(
    session_factory, session_store, org_id: uuid.UUID,
):
    """The ceiling must not touch a task that can actually spawn."""
    pid = uuid.uuid4()
    async with session_factory() as db:
        parent = ORMSession(
            id=pid, org_id=org_id, agent_id="orchestrator",
            channel="web", status="active",
            config={
                "storage_bucket": "test-bucket",
                "storage_key_prefix": "",
                "workspace_path": f"/workspace/test/{pid}",
                "supports_vision": False,
            },
        )
        db.add(parent)
        await db.commit()
        await db.refresh(parent)

    tid = await _add_ready_task(session_factory, org_id, parent)
    await _tick(session_factory, session_store)

    async with session_factory() as db:
        task = await db.get(Task, tid)
    assert task.status == "running"
    assert task.current_session_id is not None


async def test_one_dead_task_does_not_starve_its_healthy_neighbour(
    session_factory, session_store, org_id, broken_parent,
):
    """The dead row must not consume the tick budget the good row needs."""
    pid = uuid.uuid4()
    async with session_factory() as db:
        good_parent = ORMSession(
            id=pid, org_id=org_id, agent_id="orchestrator",
            channel="web", status="active",
            config={
                "storage_bucket": "test-bucket",
                "storage_key_prefix": "",
                "workspace_path": f"/workspace/test/{pid}",
                "supports_vision": False,
            },
        )
        db.add(good_parent)
        await db.commit()
        await db.refresh(good_parent)

    dead = await _add_ready_task(
        session_factory, org_id, broken_parent, max_attempts=1,
    )
    good = await _add_ready_task(session_factory, org_id, good_parent)

    await _tick(session_factory, session_store)

    async with session_factory() as db:
        assert (await db.get(Task, dead)).status == "blocked"
        assert (await db.get(Task, good)).status == "running"
