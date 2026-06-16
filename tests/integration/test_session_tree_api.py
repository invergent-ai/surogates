"""Integration tests for /v1/sessions/{id}/tree and /children."""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from surogates.db.models import Mission as MissionRow
from surogates.db.models import ScheduledSession as ScheduledSessionRow
from surogates.db.models import Session as SessionRow
from surogates.db.models import Task as TaskRow
from surogates.db.models import TaskLink
from surogates.session.store import SessionStore
from surogates.scheduled.schedule import parse_dynamic_loop_schedule
from surogates.scheduled.store import ScheduledSessionStore
from surogates.storage.backend import LocalBackend
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# The agent_id that the API-layer authorization expects on sessions.  We
# make session creation and the app settings use the same value so
# ``_get_session_for_tenant`` accepts our test sessions.
_AGENT_ID = "test-agent"


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url, tmp_path_factory):
    """FastAPI app wired to the test containers."""
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.runtime import (
        agent_runtime_context_dep,
        build_agent_runtime_context,
    )

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()

    storage_root = tmp_path_factory.mktemp("tree-api-storage")
    application.state.storage = LocalBackend(base_path=str(storage_root))
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )

    # Tree/children routes authorize sessions against the per-request
    # agent context (shared-runtime); ``agent_id`` no longer lives on
    # ``Settings``.  Pin it to the value our sessions are created with.
    def _fixed_runtime_context():
        return build_agent_runtime_context({
            "agent_id": _AGENT_ID,
            "org_id": "00000000-0000-0000-0000-000000000000",
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "",
        })

    application.dependency_overrides[agent_runtime_context_dep] = (
        _fixed_runtime_context
    )
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c


async def _tenant(session_factory) -> tuple[UUID, UUID, str]:
    org_id = await create_org(session_factory)
    user_id = uuid.uuid4()
    await create_user(session_factory, org_id, user_id=user_id)
    token = create_access_token(
        org_id, user_id,
        {"sessions:read", "sessions:write", "tools:read", "admin"},
    )
    return org_id, user_id, token


# ---------------------------------------------------------------------------
# /v1/sessions/{id}/tree
# ---------------------------------------------------------------------------


async def test_tree_returns_root_only_when_no_children(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)
    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/tree",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["nodes"][0]["id"] == str(root.id)
    assert data["nodes"][0]["depth"] == 0
    assert data["nodes"][0]["parent_id"] is None
    assert data["nodes"][0]["root_session_id"] == str(root.id)


async def test_tree_returns_nested_descendants(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)

    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        config={"coordinator": True},
    )
    child1 = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
        config={"agent_type": "researcher"},
    )
    child2 = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
    )
    grandchild = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=child1.id, channel="delegation",
        config={"agent_type": "analyzer"},
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/tree",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4

    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id[str(root.id)]["depth"] == 0
    assert by_id[str(child1.id)]["depth"] == 1
    assert by_id[str(child2.id)]["depth"] == 1
    assert by_id[str(grandchild.id)]["depth"] == 2

    # agent_type flows through to the response.
    assert by_id[str(child1.id)]["agent_type"] == "researcher"
    assert by_id[str(child2.id)]["agent_type"] is None
    assert by_id[str(grandchild.id)]["agent_type"] == "analyzer"
    assert by_id[str(child1.id)]["run_kind"] is None

    # Every node shares the root's root_session_id.
    for n in data["nodes"]:
        assert n["root_session_id"] == str(root.id)


async def test_tree_marks_dynamic_loop_runs(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)

    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )
    loop_run = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=_AGENT_ID,
        parent_id=root.id,
        channel="scheduled",
        config={
            "scheduled_session_id": str(uuid.uuid4()),
            "scheduled_dynamic_loop": True,
        },
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/tree",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    by_id = {n["id"]: n for n in resp.json()["nodes"]}

    assert by_id[str(root.id)]["run_kind"] is None
    assert by_id[str(loop_run.id)]["run_kind"] == "dynamic_loop"


async def test_tree_returns_full_root_tree_when_called_with_subagent_id(
    client: AsyncClient, session_factory, session_store,
):
    """Calling /tree with a sub-agent id returns the whole tree containing
    that sub-agent, not just its descendants.  This lets the sidebar
    anchor on whichever node the user clicked while keeping the
    sub-agent's siblings visible."""
    org_id, user_id, token = await _tenant(session_factory)

    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )
    sibling_a = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
    )
    sibling_b = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
    )

    resp = await client.get(
        f"/v1/sessions/{sibling_a.id}/tree",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()

    ids = {n["id"] for n in data["nodes"]}
    assert ids == {str(root.id), str(sibling_a.id), str(sibling_b.id)}
    assert data["total"] == 3
    for n in data["nodes"]:
        assert n["root_session_id"] == str(root.id)


async def test_tree_authorization_blocks_other_tenant(
    client: AsyncClient, session_factory, session_store,
):
    org_a, user_a, _token_a = await _tenant(session_factory)
    _, _, token_b = await _tenant(session_factory)

    root = await session_store.create_session(
        user_id=user_a, org_id=org_a, agent_id=_AGENT_ID,
    )
    resp = await client.get(
        f"/v1/sessions/{root.id}/tree",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404


async def test_tree_unknown_session_returns_404(
    client: AsyncClient, session_factory,
):
    _, _, token = await _tenant(session_factory)
    resp = await client.get(
        f"/v1/sessions/{uuid.uuid4()}/tree",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /v1/sessions/{id}/children
# ---------------------------------------------------------------------------


async def test_children_returns_direct_descendants_only(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)

    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )
    child = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
    )
    # Grandchild — should NOT appear in root's children response.
    await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=child.id, channel="delegation",
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/children",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["children"][0]["id"] == str(child.id)
    assert data["children"][0]["parent_id"] == str(root.id)


async def test_children_returns_agent_type(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)
    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )
    await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
        parent_id=root.id, channel="worker",
        config={"agent_type": "experiment-runner"},
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/children",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    children = resp.json()["children"]
    assert len(children) == 1
    assert children[0]["agent_type"] == "experiment-runner"


async def test_children_empty_when_no_descendants(
    client: AsyncClient, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)
    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id=_AGENT_ID,
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/children",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"children": [], "total": 0}


async def test_children_authorization_blocks_other_tenant(
    client: AsyncClient, session_factory, session_store,
):
    org_a, user_a, _ = await _tenant(session_factory)
    _, _, token_b = await _tenant(session_factory)
    root = await session_store.create_session(
        user_id=user_a, org_id=org_a, agent_id=_AGENT_ID,
    )

    resp = await client.get(
        f"/v1/sessions/{root.id}/children",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /v1/sessions/{id}
# ---------------------------------------------------------------------------


async def test_delete_parent_session_archives_descendants_and_cancels_schedules(
    client: AsyncClient, app, session_factory, session_store,
):
    org_id, user_id, token = await _tenant(session_factory)
    bucket = "delete-parent-session-tree"
    await app.state.storage.create_bucket(bucket)

    root = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=_AGENT_ID,
        config={"storage_bucket": bucket},
    )
    child = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=_AGENT_ID,
        parent_id=root.id,
        channel="scheduled",
        config={
            "storage_bucket": bucket,
            "scheduled_session_id": str(uuid.uuid4()),
            "scheduled_dynamic_loop": True,
        },
    )
    grandchild = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=_AGENT_ID,
        parent_id=child.id,
        channel="worker",
        config={"storage_bucket": bucket},
    )

    for session in (root, child, grandchild):
        await app.state.storage.write_text(
            bucket,
            f"{session.id}/workspace.txt",
            "delete me",
        )

    schedule_store = ScheduledSessionStore(session_factory)
    root_schedule = await schedule_store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id=_AGENT_ID,
        prompt="check bitcoin",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=root.id,
    )
    child_schedule = await schedule_store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id=_AGENT_ID,
        prompt="nested loop",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=child.id,
    )

    resp = await client.delete(
        f"/v1/sessions/{root.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 204

    async with session_factory() as db:
        session_rows = await db.execute(
            select(SessionRow)
            .where(SessionRow.id.in_([root.id, child.id, grandchild.id]))
            .order_by(SessionRow.created_at)
        )
        schedule_rows = await db.execute(
            select(ScheduledSessionRow.id)
            .where(ScheduledSessionRow.id.in_([root_schedule.id, child_schedule.id]))
        )

    sessions = session_rows.scalars().all()
    schedules = schedule_rows.scalars().all()

    assert [row.status for row in sessions] == [
        "archived",
        "archived",
        "archived",
    ]
    assert schedules == []
    for session in (root, child, grandchild):
        assert not await app.state.storage.exists(
            bucket,
            f"{session.id}/workspace.txt",
        )


async def test_delete_parent_session_also_deletes_owned_mission(
    client: AsyncClient, app, session_factory, session_store,
):
    """Deleting a session physically removes the Mission row (and its
    tasks + DAG edges) tied to that session.

    Before this fix, missions were left in ``status='active'`` forever,
    visible in the Missions UI even though the parent session was
    archived.  Workers were also leaking — Task rows still pointed at
    the soft-deleted coordinator session.
    """
    org_id, user_id, token = await _tenant(session_factory)
    bucket = "delete-parent-session-mission"
    await app.state.storage.create_bucket(bucket)

    coordinator = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=_AGENT_ID,
        config={"storage_bucket": bucket},
    )

    # Mission row tied to the coordinator session.
    mission_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(MissionRow(
            id=mission_id,
            org_id=org_id,
            user_id=user_id,
            session_id=coordinator.id,
            agent_id=_AGENT_ID,
            description="x",
            rubric="y",
        ))
        await db.commit()

    # Two mission-owned tasks with a parent → child DAG edge, plus a
    # worker session linked back to one of them via ``sessions.task_id``.
    parent_task_id = uuid.uuid4()
    child_task_id = uuid.uuid4()
    async with session_factory() as db:
        db.add_all([
            TaskRow(
                id=parent_task_id, org_id=org_id,
                parent_session_id=coordinator.id,
                goal="parent task", status="done",
                mission_id=mission_id,
            ),
            TaskRow(
                id=child_task_id, org_id=org_id,
                parent_session_id=coordinator.id,
                goal="child task", status="running",
                mission_id=mission_id,
            ),
            TaskLink(parent_id=parent_task_id, child_id=child_task_id),
        ])
        await db.commit()

    worker = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=_AGENT_ID,
        parent_id=coordinator.id,
        channel="worker",
        task_id=child_task_id,
        config={"storage_bucket": bucket},
    )

    await app.state.storage.write_text(
        bucket, f"{coordinator.id}/workspace.txt", "x",
    )
    await app.state.storage.write_text(
        bucket, f"{worker.id}/workspace.txt", "x",
    )

    resp = await client.delete(
        f"/v1/sessions/{coordinator.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204, resp.text

    async with session_factory() as db:
        sessions = (await db.execute(
            select(SessionRow).where(
                SessionRow.id.in_([coordinator.id, worker.id])
            )
        )).scalars().all()
        missions = (await db.execute(
            select(MissionRow).where(MissionRow.id == mission_id)
        )).scalars().all()
        tasks = (await db.execute(
            select(TaskRow).where(
                TaskRow.id.in_([parent_task_id, child_task_id])
            )
        )).scalars().all()
        links = (await db.execute(
            select(TaskLink).where(
                TaskLink.parent_id == parent_task_id
            )
        )).scalars().all()

    # Sessions archived (soft-delete keeps the row).
    assert all(s.status == "archived" for s in sessions)
    # ``sessions.task_id`` on the worker is NULL'd before the Task
    # DELETE so the FK doesn't reject.
    worker_after = next(s for s in sessions if s.id == worker.id)
    assert worker_after.task_id is None

    # Mission + tasks + edges are physically gone.
    assert missions == []
    assert tasks == []
    assert links == []


async def test_delete_session_without_mission_is_a_noop_for_mission_table(
    client: AsyncClient, app, session_factory, session_store,
):
    """Plain session deletion (no associated mission) must still work —
    the mission-cascade branch short-circuits when there's nothing to
    delete.  Guards against a no-op refactor silently regressing the
    happy path."""
    org_id, user_id, token = await _tenant(session_factory)
    bucket = "delete-no-mission"
    await app.state.storage.create_bucket(bucket)

    sess = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=_AGENT_ID,
        config={"storage_bucket": bucket},
    )
    await app.state.storage.write_text(
        bucket, f"{sess.id}/workspace.txt", "x",
    )

    resp = await client.delete(
        f"/v1/sessions/{sess.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204, resp.text

    async with session_factory() as db:
        row = await db.get(SessionRow, sess.id)
    assert row is not None
    assert row.status == "archived"
