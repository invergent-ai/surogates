"""Projects (``workstreams``): a user's coordinator chats, and their master sessions."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import delete, select

from surogates.db.agent_users import purge_user_account
from surogates.db.models import Session, SessionCursor, Workstream
from surogates.runtime import agent_runtime_context_dep, build_agent_runtime_context
from surogates.tenant.auth.jwt import create_service_account_session_token
from surogates.workstreams.store import WorkstreamStore

from .conftest import issue_service_account_token
from .test_devices import AGENT_ID, add_user, api  # noqa: F401  (api is a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def create(api, **body) -> dict:
    response = await api.client.post(
        "/v1/workstreams", json={"name": "Quarterly report", **body}, headers=api.auth(),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def master_of(api, project: dict):
    return await api.app.state.session_store.get_session(UUID(project["master_session_id"]))


def runtime(api, **changes) -> None:
    """Serve the agent with *changes* to its runtime config."""
    api.app.dependency_overrides[agent_runtime_context_dep] = lambda: build_agent_runtime_context({
        "agent_id": AGENT_ID,
        "org_id": str(api.org_id),
        "project_id": "test-project",
        "enabled": True,
        "version": 1,
        "storage_key_prefix": "",
        **changes,
    })


async def test_creating_a_project_makes_its_master_chat(api):
    project = await create(api, goal="The board's Q3 report", instructions="Use euros.")
    assert project["name"] == "Quarterly report"
    assert project["goal"] == "The board's Q3 report"
    assert project["instructions"] == "Use euros."
    assert project["icon"] is None
    assert (project["coordinator_tier"], project["thread_tier"]) == (None, None)
    assert project["created_at"].endswith("Z") and project["updated_at"].endswith("Z")

    master = await master_of(api, project)
    boundary = f"workstream:{project['id']}"
    assert (master.channel, master.user_id, master.parent_id) == ("web", api.user_id, None)
    assert master.title == "Quarterly report"
    assert {key: master.config.get(key) for key in (
        "coordinator", "strict_coordinator", "workstream_id", "workstream_role",
        "memory_boundary", "workspace_boundary", "system",
    )} == {
        "coordinator": True,
        "strict_coordinator": True,
        "workstream_id": project["id"],
        "workstream_role": "coordinator",
        "memory_boundary": boundary,
        "workspace_boundary": boundary,
        "system": "Project: Quarterly report\n\nGoal: The board's Q3 report\n\nUse euros.",
    }
    assert "workstream_tier" not in master.config


@pytest.mark.parametrize("goal", [None, "", "   "], ids=["absent", "empty", "blank"])
async def test_a_project_needs_only_a_name(api, goal):
    project = await create(api, **({} if goal is None else {"goal": goal}))
    assert (project["goal"], project["instructions"]) == (None, "")
    master = await master_of(api, project)
    assert master.config["system"] == "Project: Quarterly report"


async def test_a_name_in_any_script_is_kept_intact(api):
    name = "Raport trimestrial — T3 📊 日本語"
    project = await create(api, name=f"  {name}  ")
    assert project["name"] == name
    master = await master_of(api, project)
    assert master.title == name
    assert master.config["system"] == f"Project: {name}"


async def test_a_user_lists_and_reads_only_their_own_projects(api, session_factory):
    first = await create(api, name="Budget")
    second = await create(api, name="Hiring")
    listed = await api.client.get("/v1/workstreams", headers=api.auth())
    assert listed.status_code == 200, listed.text
    assert [p["name"] for p in listed.json()] == ["Hiring", "Budget"]
    assert set(listed.json()[0]) == {"id", "name", "icon", "created_at", "updated_at"}
    got = await api.client.get(f"/v1/workstreams/{first['id']}", headers=api.auth())
    assert got.status_code == 200, got.text
    assert got.json() == first

    _, their_token = await add_user(session_factory, api.org_id)
    theirs = await api.client.get("/v1/workstreams", headers=api.auth(their_token))
    assert theirs.status_code == 200, theirs.text
    assert theirs.json() == []
    hidden = await api.client.get(f"/v1/workstreams/{second['id']}", headers=api.auth(their_token))
    assert hidden.status_code == 404, hidden.text


async def test_a_session_token_has_no_projects(api, session_factory):
    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    account = await issue_service_account_token(session_factory, api.org_id)
    token = create_service_account_session_token(api.org_id, account.id, UUID(chat.json()["id"]))
    response = await api.client.get("/v1/workstreams", headers=api.auth(token))
    assert response.status_code == 403, response.text


async def test_an_unknown_project_is_not_found(api):
    response = await api.client.get(
        "/v1/workstreams/00000000-0000-0000-0000-000000000000", headers=api.auth(),
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("body", [
    {"name": ""},
    {"name": "   "},
    {"name": "x" * 257},
    {"name": "📊" * 128 + "x"},
    {"name": "Q3\x00"},
    {"goal": "x" * 2001},
    {"goal": "ok\x00"},
    {"instructions": "x" * 16_001},
    {"instructions": "📊" * 8000 + "x"},
    {"instructions": "Use euros\x00"},
], ids=[
    "empty", "blank", "long-name", "long-emoji-name", "nul-name", "long-goal", "nul-goal",
    "long-instructions", "long-emoji-instructions", "nul-instructions",
])
async def test_a_malformed_project_is_refused(api, body):
    response = await api.client.post(
        "/v1/workstreams", json={"name": "Quarterly report", **body}, headers=api.auth(),
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("instructions", ["x" * 16_000, "📊" * 8000], ids=["ascii", "emoji"])
async def test_the_longest_instructions_are_kept_whole(api, instructions):
    project = await create(api, instructions=instructions)
    assert project["instructions"] == instructions


async def test_an_agent_with_one_conversation_has_no_projects(api):
    project = await create(api)
    runtime(api, multi_session=False)
    for method, path in [
        ("POST", "/v1/workstreams"),
        ("GET", "/v1/workstreams"),
        ("GET", f"/v1/workstreams/{project['id']}"),
    ]:
        response = await api.client.request(
            method, path, json={"name": "Quarterly report"}, headers=api.auth(),
        )
        assert response.status_code == 409, (method, path, response.text)

    # Turned back on, the agent has its projects again.
    runtime(api)
    listed = await api.client.get("/v1/workstreams", headers=api.auth())
    assert [p["id"] for p in listed.json()] == [project["id"]]


async def test_the_server_offers_projects(api):
    response = await api.client.get("/v1/auth/config")
    assert response.status_code == 200, response.text
    assert response.json()["workstreams"] is True


async def test_a_master_whose_project_cannot_be_saved_is_archived(api, monkeypatch):
    async def fail(self, **values):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(WorkstreamStore, "create", fail)
    with pytest.raises(RuntimeError):
        await api.client.post("/v1/workstreams", json={"name": "Quarterly report"}, headers=api.auth())
    async with api.app.state.session_factory() as db:
        masters = (await db.scalars(select(Session).where(Session.user_id == api.user_id))).all()
    assert [master.status for master in masters] == ["archived"]


async def test_a_project_goes_with_its_master_when_ops_deletes_the_agents_sessions(api, session_factory):
    project = await create(api)
    master_id = UUID(project["master_session_id"])
    async with session_factory() as db:
        # As ops's delete_agent_data does: the session's own rows, then the session.
        await db.execute(delete(SessionCursor).where(SessionCursor.session_id == master_id))
        await db.execute(delete(Session).where(Session.id == master_id))
        await db.commit()
    async with session_factory() as db:
        assert await db.scalar(select(Workstream).where(Workstream.id == UUID(project["id"]))) is None


async def test_deleting_the_user_deletes_their_projects(api, session_factory):
    project = await create(api)
    async with session_factory() as db:
        await purge_user_account(db, org_id=api.org_id, user_id=api.user_id)
        await db.commit()
    async with session_factory() as db:
        assert await db.scalar(select(Workstream).where(Workstream.id == UUID(project["id"]))) is None
