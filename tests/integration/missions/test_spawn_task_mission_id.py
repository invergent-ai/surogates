"""When a session has an active mission, spawn_task stamps mission_id."""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from surogates.db.models import Session as ORMSession, Task
from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_stamps_mission_id_when_active_mission(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A spawn_task call from a session with active_mission_id sets
    tasks.mission_id on the new row."""
    from surogates.tasks.tools import _spawn_task_handler

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    mission_id = created.mission_id

    redis = AsyncMock(zadd=AsyncMock(), publish=AsyncMock())
    tenant = MagicMock(org_id=org_id, user_id=user_id)
    # Force the dependency-less path (no parents) so the Task is created
    # in ready/running state — we only need the row inserted to assert on.
    result = await _spawn_task_handler(
        {"goal": "research", "parents": []},
        session_store=session_store, redis=redis, tenant=tenant,
        session_id=str(chat_session.id), session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "task_id" in parsed, f"spawn_task failed: {parsed}"
    task_id = uuid.UUID(parsed["task_id"])

    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.mission_id == mission_id


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_leaves_mission_id_null_for_non_mission_session(
    session_factory, session_store, org_id, user_id,
):
    """A session without active_mission_id produces tasks with mission_id=None."""
    from surogates.tasks.tools import _spawn_task_handler

    from tests.integration.missions.conftest import _session_workspace_config

    sid = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=sid, org_id=org_id, user_id=user_id, agent_id="orchestrator",
            channel="web", status="active",
            config=_session_workspace_config(sid),
        ))
        await db.commit()

    redis = AsyncMock(zadd=AsyncMock(), publish=AsyncMock())
    tenant = MagicMock(org_id=org_id, user_id=user_id)
    result = await _spawn_task_handler(
        {"goal": "research", "parents": []},
        session_store=session_store, redis=redis, tenant=tenant,
        session_id=str(sid), session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "task_id" in parsed, f"spawn_task failed: {parsed}"
    task_id = uuid.UUID(parsed["task_id"])

    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.mission_id is None
