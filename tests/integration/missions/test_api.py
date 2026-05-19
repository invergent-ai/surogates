"""Integration tests for the missions REST API.

Routes live under ``/v1/missions``. Every request is bearer-authenticated;
``user_session.auth_headers`` carries an org/user-scoped JWT.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from surogates.db.models import Task
from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore
from surogates.session.events import EventType

from tests.integration.inbox_e2e_helpers import create_user_token_session


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_detail(inbox_app, session_factory, session_store):
    """GET /v1/missions/{id} returns the mission summary."""
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=user_session.session.id,
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/v1/missions/{created.mission_id}",
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(created.mission_id)
    assert body["status"] == "active"
    assert body["iteration"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_tasks(inbox_app, session_factory, session_store):
    """GET /v1/missions/{id}/tasks returns the mission task DAG."""
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=user_session.session.id,
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with session_factory() as db:
        db.add_all([
            Task(
                org_id=user_session.org_id,
                parent_session_id=user_session.session.id,
                goal="r1", status="done", mission_id=created.mission_id,
            ),
            Task(
                org_id=user_session.org_id,
                parent_session_id=user_session.session.id,
                goal="r2", status="running", mission_id=created.mission_id,
            ),
        ])
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/v1/missions/{created.mission_id}/tasks",
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["tasks"]) == 2
    statuses = sorted(t["status"] for t in body["tasks"])
    assert statuses == ["done", "running"]


@pytest.mark.asyncio(loop_scope="session")
async def test_get_missions_list_filters_by_user_and_status(
    inbox_app, session_factory, session_store,
):
    """GET /v1/missions filters by org+user and (optionally) status/agent_id."""
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=user_session.session.id,
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            "/v1/missions",
            params={"status": "active", "agent_id": "orchestrator"},
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [m["id"] for m in body["missions"]]
    assert str(created.mission_id) in ids


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_rejects_cross_tenant_access(
    inbox_app, session_factory, session_store,
):
    """A mission for org A is invisible to user in org B (404)."""
    owner = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    intruder = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=owner.session.id,
        user_id=owner.user_id, org_id=owner.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/v1/missions/{created.mission_id}",
            headers=intruder.auth_headers,
        )
    assert resp.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_post_pause_transitions_to_paused(
    inbox_app, session_factory, session_store,
):
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=user_session.session.id,
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/v1/missions/{created.mission_id}/pause",
            json={"reason": "manual"},
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 200, resp.text
    m = await store.get(created.mission_id)
    assert m.status == "paused"
    assert m.paused_reason == "manual"


@pytest.mark.asyncio(loop_scope="session")
async def test_post_cancel_with_cascade_marks_tasks_cancelled(
    inbox_app, session_factory, session_store,
):
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=user_session.session.id,
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with session_factory() as db:
        db.add(Task(
            org_id=user_session.org_id,
            parent_session_id=user_session.session.id,
            goal="t", status="ready", mission_id=created.mission_id,
        ))
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/v1/missions/{created.mission_id}/cancel",
            json={"reason": "abort", "cascade_to_workers": True},
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 200, resp.text
    m = await store.get(created.mission_id)
    assert m.status == "cancelled"
    async with session_factory() as db:
        from sqlalchemy import select
        tasks = (await db.execute(
            select(Task).where(Task.mission_id == created.mission_id)
        )).scalars().all()
        statuses = [t.status for t in tasks]
        assert "cancelled" in statuses


@pytest.mark.asyncio(loop_scope="session")
async def test_post_resume_returns_active(
    inbox_app, session_factory, session_store,
):
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=user_session.session.id,
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    await store.set_status(created.mission_id, "paused", paused_reason="x")

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/v1/missions/{created.mission_id}/resume",
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 200, resp.text
    m = await store.get(created.mission_id)
    assert m.status == "active"


# ---------------------------------------------------------------------------
# GET /missions/{id}/workers — merges task-backed children with direct
# spawn_worker / delegate_task children of the coordinator session.
# ---------------------------------------------------------------------------


async def _setup_mission_with_children(session_factory, session_store):
    """Set up a mission whose coordinator has all three child shapes:
    a task-backed worker, a spawn_worker direct child, and a
    delegate_task direct child. Returns the data the workers tests
    need."""
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=user_session.session.id,
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )

    # Spawn a task-backed worker (the spawn_task path).
    task_session = await session_store.create_session(
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator", channel="web", parent_id=user_session.session.id,
    )
    async with session_factory() as db:
        task = Task(
            org_id=user_session.org_id,
            parent_session_id=user_session.session.id,
            goal="task-backed work", status="running",
            mission_id=created.mission_id,
            current_session_id=task_session.id,
            agent_def_name="researcher",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    # Direct child via spawn_worker (channel='worker', durable async).
    worker_session = await session_store.create_session(
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator", channel="worker",
        parent_id=user_session.session.id,
    )
    # Direct child via delegate_task (channel='delegation', sync fork-join).
    delegation_session = await session_store.create_session(
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator", channel="delegation",
        parent_id=user_session.session.id,
    )

    # Emit one event on each child so latest_event_* fields populate.
    await session_store.emit_event(
        worker_session.id, EventType.USER_MESSAGE,
        {"content": "worker prompt"},
    )
    await session_store.emit_event(
        delegation_session.id, EventType.USER_MESSAGE,
        {"content": "delegation prompt"},
    )

    return {
        "user_session": user_session,
        "mission_id": created.mission_id,
        "task_id": task_id,
        "task_session_id": task_session.id,
        "worker_session_id": worker_session.id,
        "delegation_session_id": delegation_session.id,
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_workers_includes_all_three_child_kinds(
    inbox_app, session_factory, session_store,
):
    """The workers endpoint must surface spawn_task / spawn_worker /
    delegate_task children — kind='task', 'worker', 'delegation'
    respectively. Without the merge, spawn_worker + delegate_task
    children were invisible in the Mission UI even when the coordinator
    was actively delegating (the PROD failure mode this fixes)."""
    setup = await _setup_mission_with_children(session_factory, session_store)

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/v1/missions/{setup['mission_id']}/workers",
            headers=setup["user_session"].auth_headers,
        )
    assert resp.status_code == 200, resp.text
    workers = resp.json()["workers"]

    by_kind: dict[str, list[dict]] = {}
    for w in workers:
        by_kind.setdefault(w["kind"], []).append(w)

    # Exactly one of each kind.
    assert sorted(by_kind.keys()) == ["delegation", "task", "worker"]
    assert len(by_kind["task"]) == 1
    assert len(by_kind["worker"]) == 1
    assert len(by_kind["delegation"]) == 1

    # Task-backed entry keeps the legacy shape (task_id + task_status set).
    task_entry = by_kind["task"][0]
    assert task_entry["task_id"] == str(setup["task_id"])
    assert task_entry["task_status"] == "running"
    assert task_entry["worker_session_id"] == str(setup["task_session_id"])
    assert task_entry["agent_def_name"] == "researcher"

    # spawn_worker child: kind='worker', no task_id/task_status.
    worker_entry = by_kind["worker"][0]
    assert worker_entry["task_id"] is None
    assert worker_entry["task_status"] is None
    assert worker_entry["worker_session_id"] == str(setup["worker_session_id"])
    assert worker_entry["latest_event_kind"] == EventType.USER_MESSAGE.value

    # delegate_task child: kind='delegation', no task_id/task_status.
    deleg_entry = by_kind["delegation"][0]
    assert deleg_entry["task_id"] is None
    assert deleg_entry["task_status"] is None
    assert deleg_entry["worker_session_id"] == str(setup["delegation_session_id"])
    assert deleg_entry["latest_event_kind"] == EventType.USER_MESSAGE.value


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_workers_ignores_unrelated_child_channels(
    inbox_app, session_factory, session_store,
):
    """Only ``worker`` and ``delegation`` channels of direct children
    show up. A scheduled child (or any other channel) of the
    coordinator session must not leak into the mission's worker list —
    that filtering keeps unrelated activity (e.g. a /loop fire that
    happens to inherit parent_id) out of mission views."""
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=user_session.session.id,
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )

    # A scheduled child — different channel, should NOT appear.
    await session_store.create_session(
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator", channel="scheduled",
        parent_id=user_session.session.id,
    )

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/v1/missions/{created.mission_id}/workers",
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["workers"] == []
