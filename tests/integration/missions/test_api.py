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
