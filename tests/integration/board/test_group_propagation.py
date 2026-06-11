"""Coordination-group formation across the three spawn paths."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from surogates.board.groups import ensure_group_and_inherit
from surogates.db.models import Session as ORMSession, Task
from surogates.session.store import SessionStore


@pytest.mark.asyncio(loop_scope="session")
async def test_helper_self_assigns_inherits_and_mirrors_live_config(
    session_factory, parent_session,
):
    store = SessionStore(session_factory)
    live = dict(parent_session.config)
    child_config: dict = {}

    gid = await ensure_group_and_inherit(
        parent_session=parent_session,
        session_store=store,
        child_config=child_config,
        live_parent_config=live,
    )
    assert gid == str(parent_session.id)
    assert child_config["context_group_id"] == gid
    # Live (in-wake) dict mirrored so the same wake's board hook sees it.
    assert live["context_group_id"] == gid
    # Persisted on the parent row.
    refreshed = await store.get_session(parent_session.id)
    assert refreshed.config["context_group_id"] == gid

    # Second spawn inherits the SAME group — no new group formed.
    child_config2: dict = {}
    gid2 = await ensure_group_and_inherit(
        parent_session=refreshed,
        session_store=store,
        child_config=child_config2,
        live_parent_config=None,
    )
    assert gid2 == gid
    assert child_config2["context_group_id"] == gid


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_worker_path_stamps_group(
    session_factory, parent_session,
):
    from surogates.tools.builtin.coordinator import _spawn_worker_handler

    store = SessionStore(session_factory)
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    redis.publish = AsyncMock()
    tenant = MagicMock(org_id=parent_session.org_id, user_id=None)
    live_config = dict(parent_session.config)

    result = json.loads(await _spawn_worker_handler(
        {"goal": "inspect channels"},
        session_store=store,
        redis=redis,
        tenant=tenant,
        session_id=str(parent_session.id),
        budget=SimpleNamespace(remaining=20),
        session_factory=session_factory,
        session_config=live_config,
    ))
    assert "error" not in result, result
    worker_id = uuid.UUID(result["worker_id"])

    child = await store.get_session(worker_id)
    assert child.config["context_group_id"] == str(parent_session.id)
    # Parent persisted + live dict mirrored (same-wake board read path).
    refreshed = await store.get_session(parent_session.id)
    assert refreshed.config["context_group_id"] == str(parent_session.id)
    assert live_config["context_group_id"] == str(parent_session.id)


@pytest.mark.asyncio(loop_scope="session")
async def test_task_path_stamps_group_and_retry_rejoins(
    session_factory, org_id, parent_session,
):
    from surogates.tasks.spawn import _create_session_for_task

    store = SessionStore(session_factory)
    tenant = MagicMock(org_id=org_id, user_id=None)

    async with session_factory() as db:
        task = Task(
            org_id=org_id,
            parent_session_id=parent_session.id,
            goal="audit the slack adapter",
            status="running",
            attempt_count=1,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    child1 = await _create_session_for_task(
        task, session_store=store,
        session_factory=session_factory, tenant=tenant,
    )
    assert child1.config["context_group_id"] == str(parent_session.id)

    # Retry attempt flows through the same function and rejoins.
    async with session_factory() as db:
        row = await db.get(Task, task.id)
        row.attempt_count = 2
        await db.commit()
        await db.refresh(row)
    child2 = await _create_session_for_task(
        row, session_store=store,
        session_factory=session_factory, tenant=tenant,
    )
    assert child2.config["context_group_id"] == str(parent_session.id)
    assert child2.id != child1.id


@pytest.mark.asyncio(loop_scope="session")
async def test_delegate_child_config_inherits_group(
    session_factory, parent_session,
):
    # The delegate path calls the same helper with the parent loaded
    # fresh from the store; assert the handler-level wiring by driving
    # ensure_group_and_inherit exactly as _run_single_delegation does.
    store = SessionStore(session_factory)
    parent = await store.get_session(parent_session.id)
    child_config: dict = {
        "max_iterations": 10,
        "streaming": False,
        "delegation_depth": 1,
        "delegation_role": "leaf",
    }
    gid = await ensure_group_and_inherit(
        parent_session=parent,
        session_store=store,
        child_config=child_config,
        live_parent_config=None,
    )
    assert child_config["context_group_id"] == gid == str(parent_session.id)
