"""Integration tests for the missions REST API.

Routes live under ``/v1/missions``. Every request is bearer-authenticated;
``user_session.auth_headers`` carries an org/user-scoped JWT.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore
from surogates.session.events import EventType

from tests.integration.inbox_e2e_helpers import create_user_token_session
from tests.integration.missions.conftest import a_mission_agent


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
        mission_store=store,
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
async def test_the_tasks_endpoint_is_gone(inbox_app, session_factory, session_store):
    """A mission's work is its agents (``/workers``): there are no task rows to list."""
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
        mission_store=store,
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/v1/missions/{created.mission_id}/tasks",
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 404


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
        mission_store=store,
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
        mission_store=store,
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
        mission_store=store,
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
async def test_post_cancel_with_cascade_stops_the_missions_agents(
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
        mission_store=store,
    )
    running = await a_mission_agent(session_factory, user_session.session, created.mission_id, status="active")

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
    assert (await session_store.get_session(running.id)).status == "paused"


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
        mission_store=store,
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
# GET /missions/{id}/workers — the mission's agents: the sessions its
# coordinator started with ``agent`` while the mission was active.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_workers_lists_the_missions_agents(
    inbox_app, session_factory, session_store,
):
    import uuid as _uuid

    from surogates.db.models import Session as SessionRow

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
        mission_store=store,
    )
    analyst = await a_mission_agent(
        session_factory, user_session.session, created.mission_id,
        status="active", description="Count rows", agent_type="analyst",
    )
    finished = await a_mission_agent(session_factory, user_session.session, created.mission_id, status="completed")
    # Children that are not the mission's agents stay out: an agent of no mission, and an old worker.
    async with session_factory() as db:
        for channel, config in (("delegation", {"agent_dispatch": {"mode": "background"}}), ("worker", {})):
            db.add(SessionRow(
                id=_uuid.uuid4(), org_id=user_session.org_id, agent_id="orchestrator", channel=channel,
                parent_id=user_session.session.id, config=config,
            ))
        await db.commit()
    await session_store.emit_event(analyst.id, EventType.USER_MESSAGE, {"content": "count"})

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/v1/missions/{created.mission_id}/workers",
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["workers"]
    assert [row["worker_session_id"] for row in rows] == [str(analyst.id), str(finished.id)]
    first = rows[0]
    assert (first["agent_def_name"], first["description"], first["session_status"]) == ("analyst", "Count rows", "active")
    assert first["latest_event_kind"] == EventType.USER_MESSAGE.value
    assert first["transcript_url"] == f"/chat/{analyst.id}"
    assert not {"kind", "task_id", "task_status"} & set(first)


async def _seed_mission_event_graph(session_factory, session_store, user_session):
    """Create a mission with one agent, plus four events across the two sessions (one of which is a hidden
    ``llm.delta`` chunk)."""
    from surogates.db.models import Event as EventRow

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=user_session.session.id,
        user_id=user_session.user_id, org_id=user_session.org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store,
    )
    agent = await a_mission_agent(
        session_factory, user_session.session, created.mission_id, status="active", agent_type="claude-coder",
    )
    # Separate commits pin the bigserial id order the cursor pages over.
    async with session_factory() as db:
        db.add(EventRow(
            session_id=user_session.session.id, org_id=user_session.org_id,
            type="delegation.start", data={"child_session_id": str(agent.id), "goal": "g", "agent": True},
        ))
        await db.commit()
    async with session_factory() as db:
        db.add(EventRow(
            session_id=agent.id, org_id=user_session.org_id,
            type="iteration.summary", data={"summary": "doing work"},
        ))
        await db.commit()
    async with session_factory() as db:
        db.add(EventRow(
            session_id=agent.id, org_id=user_session.org_id,
            type="llm.delta", data={"chunk": "x"},
        ))
        db.add(EventRow(
            session_id=agent.id, org_id=user_session.org_id,
            type="tool.call", data={"name": "bash"},
        ))
        await db.commit()
    return created, agent.id


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_events_merges_and_labels_sessions(
    inbox_app, session_factory, session_store,
):
    """GET /v1/missions/{id}/events merges the coordinator's and its agents'
    events ascending by id, hides llm.delta, and labels every contributing
    session in the ``sessions`` map."""
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    created, agent_session_id = await _seed_mission_event_graph(
        session_factory, session_store, user_session,
    )

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/v1/missions/{created.mission_id}/events",
            headers=user_session.auth_headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # handle_mission_create emits mission.defined on the coordinator
    # session, so the feed opens with it; llm.delta stays hidden.
    types = [e["type"] for e in body["events"]]
    assert types == [
        "mission.defined", "delegation.start", "iteration.summary", "tool.call",
    ]
    ids = [e["id"] for e in body["events"]]
    assert ids == sorted(ids)

    sessions = body["sessions"]
    assert sessions == {
        str(user_session.session.id): {"agent_def_name": None, "kind": "coordinator"},
        str(agent_session_id): {"agent_def_name": "claude-coder", "kind": "agent"},
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_events_after_id_cursor(
    inbox_app, session_factory, session_store,
):
    """``after_id`` pages strictly forward; ``limit`` bounds the page."""
    user_session = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    created, *_ = await _seed_mission_event_graph(
        session_factory, session_store, user_session,
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        first = await client.get(
            f"/v1/missions/{created.mission_id}/events?limit=1",
            headers=user_session.auth_headers,
        )
        assert first.status_code == 200, first.text
        events = first.json()["events"]
        assert len(events) == 1
        rest = await client.get(
            f"/v1/missions/{created.mission_id}/events"
            f"?after_id={events[0]['id']}",
            headers=user_session.auth_headers,
        )
    assert rest.status_code == 200, rest.text
    # 4 visible events total (mission.defined + the three seeded ones);
    # the cursor page excludes the first.
    rest_ids = [e["id"] for e in rest.json()["events"]]
    assert len(rest_ids) == 3
    assert all(i > events[0]["id"] for i in rest_ids)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_events_cross_tenant_404(
    inbox_app, session_factory, session_store,
):
    """A user from another org gets 404, not an empty page — same
    contract as every other mission read."""
    owner = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    created, *_ = await _seed_mission_event_graph(
        session_factory, session_store, owner,
    )
    stranger = await create_user_token_session(
        session_factory, session_store, agent_id="orchestrator",
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/v1/missions/{created.mission_id}/events",
            headers=stranger.auth_headers,
        )
    assert resp.status_code == 404, resp.text
