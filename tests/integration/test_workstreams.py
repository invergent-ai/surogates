"""Projects (``workstreams``): a user's coordinator chats, and their master sessions."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

import surogates.harness.loop as loop_module
from surogates.db.agent_users import purge_user_account
from surogates.db.models import ScheduledSession, Session, SessionCursor, Workstream
from surogates.harness.prompt import PromptBuilder
from surogates.harness.turn_summarizer import TurnSummary
from surogates.runtime import (
    SLASH_COMMAND_IDS,
    SlashCommandConfig,
    agent_runtime_context_dep,
    build_agent_runtime_context,
)
from surogates.runtime.rate_limiter import PerTenantRateLimiter
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_service_account_session_token
from surogates.tenant.context import TenantContext
from surogates.tools.loader import AgentDef
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from surogates.workstreams.store import WorkstreamStore
from tests.test_harness_resilience import _make_harness
from tests.test_steer_loop import _final_response, _make_loop_harness
from tests.test_wake_slash_command_gate import (
    _harness,
    _llm_responses,
    _permissive,
    _stub_store,
    _user_event,
)

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


async def test_a_name_is_stripped_as_the_chat_title_is(api):
    # Python's strip, which the chat title uses, also takes U+001C-U+001F.
    project = await create(api, name="\x1fQ3\x1f")
    master = await master_of(api, project)
    assert (project["name"], master.title, master.config["system"]) == ("Q3", "Q3", "Project: Q3")

    renamed = await patch(api, project, {"name": "\x1eQ4\x1e"})
    assert renamed.status_code == 200, renamed.text
    master = await master_of(api, project)
    assert (renamed.json()["name"], master.title, master.config["system"]) == ("Q4", "Q4", "Project: Q4")


async def test_creating_a_project_counts_against_the_agents_rate_limit(api):
    api.app.state.rate_limiter = PerTenantRateLimiter(api.app.state.redis)
    runtime(api, governance={"rate_limit_rpm": 1})
    await create(api)
    response = await api.client.post("/v1/workstreams", json={"name": "Budget"}, headers=api.auth())
    assert response.status_code == 429, response.text


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
    {"name": "\x1c\x1d\x1e\x1f"},
    {"goal": "x" * 2001},
    {"goal": "ok\x00"},
    {"instructions": "x" * 16_001},
    {"instructions": "📊" * 8000 + "x"},
    {"instructions": "Use euros\x00"},
], ids=[
    "empty", "blank", "long-name", "long-emoji-name", "nul-name", "separators-name", "long-goal",
    "nul-goal", "long-instructions", "long-emoji-instructions", "nul-instructions",
])
async def test_a_malformed_project_is_refused(api, body):
    response = await api.client.post(
        "/v1/workstreams", json={"name": "Quarterly report", **body}, headers=api.auth(),
    )
    assert response.status_code == 422, response.text
    async with api.app.state.session_factory() as db:
        assert (await db.scalars(select(Session.id).where(Session.user_id == api.user_id))).all() == []


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


@pytest.mark.parametrize("step", [
    (WorkstreamStore, "create"), (SessionStore, "update_session_title"),
], ids=["row", "title"])
async def test_a_master_whose_project_cannot_be_saved_is_archived(api, monkeypatch, step):
    async def fail(self, *args, **values):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(*step, fail)
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


async def patch(api, project: dict, body: dict, token: str | None = None):
    return await api.client.patch(f"/v1/workstreams/{project['id']}", json=body, headers=api.auth(token))


async def test_changing_a_project_writes_through_to_its_master(api):
    project = await create(api, goal="Q3", instructions="Use euros.")
    response = await patch(api, project, {
        "name": "Annual report", "icon": "chart", "goal": None, "instructions": "Use dollars.",
        "coordinator_tier": "pro", "thread_tier": "basic",
    })
    assert response.status_code == 200, response.text
    changed = response.json()
    assert {k: changed[k] for k in ("name", "icon", "goal", "instructions", "coordinator_tier", "thread_tier")} == {
        "name": "Annual report", "icon": "chart", "goal": None, "instructions": "Use dollars.",
        "coordinator_tier": "pro", "thread_tier": "basic",
    }
    master = await master_of(api, project)
    assert master.title == "Annual report"
    assert master.config["system"] == "Project: Annual report\n\nUse dollars."
    assert master.config["workstream_tier"] == "pro"

    cleared = await patch(api, project, {"coordinator_tier": None})
    assert cleared.status_code == 200, cleared.text
    assert "workstream_tier" not in (await master_of(api, project)).config


async def test_a_change_moves_a_project_up_the_list(api):
    project = await create(api)
    later = await create(api, name="Budget")
    response = await patch(api, project, {"icon": "chart"})
    assert datetime.fromisoformat(response.json()["updated_at"]) > datetime.fromisoformat(project["updated_at"])
    listed = await api.client.get("/v1/workstreams", headers=api.auth())
    assert [p["id"] for p in listed.json()] == [project["id"], later["id"]]


async def test_an_emptied_goal_or_icon_is_cleared(api):
    project = await create(api, goal="Q3")
    await patch(api, project, {"icon": "chart"})
    response = await patch(api, project, {"goal": "", "icon": " "})
    assert response.status_code == 200, response.text
    assert (response.json()["goal"], response.json()["icon"]) == (None, None)
    assert (await master_of(api, project)).config["system"] == "Project: Quarterly report"


async def test_a_change_leaves_out_what_it_does_not_name(api):
    project = await create(api, goal="Q3", instructions="Use euros.")
    response = await patch(api, project, {"icon": "chart"})
    assert response.status_code == 200, response.text
    assert (response.json()["goal"], response.json()["instructions"]) == ("Q3", "Use euros.")
    assert (await master_of(api, project)).config["system"] == "Project: Quarterly report\n\nGoal: Q3\n\nUse euros."


@pytest.mark.parametrize("body", [
    {"name": None},
    {"instructions": None},
    {"name": "  "},
    {"name": "\x1c\x1d\x1e\x1f"},
    {"icon": "x" * 65},
    {"coordinator_tier": "max"},
    {"thread_tier": "fast"},
], ids=[
    "null-name", "null-instructions", "blank-name", "separators-name", "long-icon",
    "coordinator-tier", "thread-tier",
])
async def test_a_malformed_change_is_refused(api, body):
    project = await create(api)
    response = await patch(api, project, body)
    assert response.status_code == 422, response.text


async def test_an_agent_with_one_conversation_leaves_its_projects_alone(api):
    project = await create(api)
    runtime(api, multi_session=False)
    assert (await patch(api, project, {"name": "Mine"})).status_code == 409
    deleted = await api.client.delete(f"/v1/workstreams/{project['id']}", headers=api.auth())
    assert deleted.status_code == 409, deleted.text
    runtime(api)
    assert (await api.client.get(f"/v1/workstreams/{project['id']}", headers=api.auth())).json() == project


async def test_another_users_project_cannot_be_changed_or_archived(api, session_factory):
    project = await create(api)
    _, their_token = await add_user(session_factory, api.org_id)
    assert (await patch(api, project, {"name": "Mine"}, their_token)).status_code == 404
    deleted = await api.client.delete(f"/v1/workstreams/{project['id']}", headers=api.auth(their_token))
    assert deleted.status_code == 404, deleted.text
    assert (await master_of(api, project)).status == "active"


async def test_archiving_a_project_archives_its_master_and_keeps_its_files(api):
    project = await create(api)
    master = await master_of(api, project)
    uploaded = await api.client.post(
        f"/v1/sessions/{master.id}/workspace/upload",
        files={"file": ("brief.docx", b"the brief")},
        headers=api.auth(),
    )
    assert uploaded.status_code == 201, uploaded.text

    response = await api.client.delete(f"/v1/workstreams/{project['id']}", headers=api.auth())
    assert response.status_code == 204, response.text
    assert (await master_of(api, project)).status == "archived"
    assert (await api.client.get(f"/v1/workstreams/{project['id']}", headers=api.auth())).status_code == 404
    assert (await api.client.get("/v1/workstreams", headers=api.auth())).json() == []
    assert (await patch(api, project, {"name": "Again"})).status_code == 404
    keys = await api.app.state.storage.list_keys(
        master.config["storage_bucket"], f"boundaries/workstream:{project['id']}/workspace/",
    )
    assert any(key.endswith("brief.docx") for key in keys)


async def test_a_user_archives_or_renames_a_master_only_through_its_project(api):
    project = await create(api)
    chat = f"/v1/sessions/{project['master_session_id']}"
    deleted = await api.client.delete(chat, headers=api.auth())
    assert deleted.status_code == 409, deleted.text
    renamed = await api.client.patch(chat, json={"title": "Mine"}, headers=api.auth())
    assert renamed.status_code == 409, renamed.text
    master = await master_of(api, project)
    assert (master.status, master.title) == ("active", "Quarterly report")


async def test_ops_archiving_a_master_archives_its_project(api, session_factory):
    # Studio's archive and the first step of its delete send this request.
    project = await create(api)
    account = await issue_service_account_token(session_factory, api.org_id)
    response = await api.client.delete(
        f"/v1/api/sessions/{project['master_session_id']}",
        headers={"Authorization": f"Bearer {account.token}"},
    )
    assert response.status_code == 204, response.text
    assert (await master_of(api, project)).status == "archived"
    async with session_factory() as db:
        row = await db.scalar(select(Workstream).where(Workstream.id == UUID(project["id"])))
    assert row.status == "archived"
    assert (await api.client.get("/v1/workstreams", headers=api.auth())).json() == []


async def test_a_master_is_not_in_the_chat_list(api):
    project = await create(api)
    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    assert chat.status_code == 201, chat.text
    listed = await api.client.get("/v1/sessions", headers=api.auth())
    assert listed.status_code == 200, listed.text
    assert [s["id"] for s in listed.json()["sessions"]] == [chat.json()["id"]]
    opened = await api.client.get(f"/v1/sessions/{project['master_session_id']}", headers=api.auth())
    assert opened.status_code == 200, opened.text


async def test_config_cannot_make_a_chat_part_of_a_project(api):
    project = await create(api)
    response = await api.client.post("/v1/sessions", json={"config": {
        "workstream_id": project["id"], "workstream_role": "coordinator", "workstream_tier": "pro",
    }}, headers=api.auth())
    assert response.status_code == 201, response.text
    config = response.json()["config"]
    assert not {"workstream_id", "workstream_role", "workstream_tier"} & set(config)
    listed = await api.client.get("/v1/sessions", headers=api.auth())
    assert [s["id"] for s in listed.json()["sessions"]] == [response.json()["id"]]


def harness_with_every_tool():
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    return _make_harness(tool_registry=registry, prompt_builder=SimpleNamespace(has_agents=False))


ROUTINE_TOOLS = {"cron_create", "cron_list", "cron_delete"}


@pytest.mark.parametrize("loop", [True, False], ids=["loop-on", "loop-off"])
async def test_a_master_reads_and_coordinates_but_does_no_work_itself(api, monkeypatch, loop):
    master = await master_of(api, await create(api))
    sent, _, _ = await turn_calling(monkeypatch, master, {}, loop=loop)
    assert {
        "read_file", "search_files", "list_files", "kb_list_pages", "kb_read_page", "kb_search_pages",
        "memory", "todo", "ask_user_question", "session_search", "skills_list", "skill_view",
    } <= sent
    # Its routines follow the agent's /loop.
    assert sent & ROUTINE_TOOLS == (ROUTINE_TOOLS if loop else set())
    assert not sent & {
        "write_file", "patch", "terminal", "web_search", "browser_navigate", "create_artifact",
        "spawn_worker", "send_worker_message", "stop_worker", "delegate_task",
        "spawn_task", "unblock_task", "cancel_task", "run_coding_agent",
    }


async def test_a_mission_coordinator_keeps_its_own_tools(api):
    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    session = await api.app.state.session_store.get_session(UUID(chat.json()["id"]))
    session.config.update(coordinator=True, strict_coordinator=True)
    tools = harness_with_every_tool()._tool_filter_for_session(session)
    assert {"spawn_worker", "delegate_task", "spawn_task", "run_coding_agent"} <= tools
    assert not tools & {"read_file", "kb_read_page"}


def woken_on(monkeypatch, session, message: str):
    """A harness that wakes *session* on *message*, its command handlers stubbed."""
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    store = _stub_store(session, [_user_event(10, message)])
    harness = _harness(store, _permissive())
    for handler in (
        "_handle_goal_command", "_handle_mission_command", "_handle_auto_research_command",
        "_handle_code_command", "_handle_loop_command", "_run_loop",
    ):
        setattr(harness, handler, AsyncMock())
    return harness, store


@pytest.mark.parametrize("message, handler", [
    ("/goal Ship the Q3 report", "_handle_goal_command"),
    ("/mission Audit the Q3 figures", "_handle_mission_command"),
    ("/auto-research Find the best forecast", "_handle_auto_research_command"),
    ("/code Fix the totals script", "_handle_code_command"),
    ("/deep-research The Q3 market", "_run_loop"),
], ids=["goal", "mission", "auto-research", "code", "deep-research"])
async def test_a_master_refuses_the_commands_that_work_in_place(api, monkeypatch, message, handler):
    master = await master_of(api, await create(api))
    harness, store = woken_on(monkeypatch, master, message)
    await harness.wake(master.id)
    command = message.split()[0]
    assert _llm_responses(store) == [
        f"{command} does not run in a project's conversation. Ask for the work here, and it is given to a thread."
    ]
    getattr(harness, handler).assert_not_awaited()


async def test_a_master_keeps_its_routines(api, monkeypatch):
    master = await master_of(api, await create(api))
    harness, store = woken_on(monkeypatch, master, "/loop 1d Check the cash report")
    await harness.wake(master.id)
    harness._handle_loop_command.assert_awaited_once()
    assert _llm_responses(store) == []


async def test_a_master_refuses_a_goal_the_web_client_sends_as_an_event(api):
    # The web composer turns "/goal <text>" into this event, never a message.
    master = await master_of(api, await create(api))
    response = await api.client.post(
        f"/v1/sessions/{master.id}/events",
        json={"events": [{
            "type": "user.define_outcome",
            "description": "Ship the Q3 report",
            "rubric": {"type": "text", "content": "- the report is filed"},
        }]},
        headers=api.auth(),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "/goal does not run in a project's conversation. Ask for the work here, and it is given to a thread."
    )
    assert "outcome" not in (await api.app.state.session_store.get_session(master.id)).config


async def turn_calling(
    monkeypatch, session, calls: dict[str, dict], *, streamed=False, loop=True, session_factory=None,
):
    """One turn of *session* whose model makes *calls* (name: arguments).

    Returns the tools the model was sent, the dispatch, and each call's
    answer.  The dispatch is a stub unless *session_factory* is given.
    """
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    ran = AsyncMock(side_effect=registry.dispatch) if session_factory else AsyncMock(return_value='{"ok": true}')
    monkeypatch.setattr(registry, "dispatch", ran)
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(session_store=store)
    harness._tools = registry
    harness._tenant = SimpleNamespace(org_id=session.org_id, user_id=session.user_id, asset_root="/tmp/test")
    harness._streaming_enabled = streamed
    harness._slash_commands = SlashCommandConfig() if loop else SlashCommandConfig(
        commands=SLASH_COMMAND_IDS - {"loop"},
    )
    harness._session_factory = session_factory
    tool_calls = [
        {"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for name, args in calls.items()
    ]
    responses = iter([
        ({"role": "assistant", "content": "", "tool_calls": tool_calls},
         {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1}),
    ] * bool(calls) + [_final_response("Done.")])
    sent: list[set[str]] = []

    async def llm(**kwargs):
        sent.append({schema["function"]["name"] for schema in kwargs["create_kwargs"]["tools"]})
        message, usage = next(responses)
        if kwargs["on_tool_call_complete"] is not None:
            for call in message["tool_calls"] or []:
                kwargs["on_tool_call_complete"](call)
        return message, usage

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    messages = [{"role": "user", "content": "Get the Q3 report done"}]
    await harness._run_loop(session, messages, "system", SimpleNamespace(lease_token=uuid4()), all_events=[])
    answered = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
    return sent[0], ran, answered


def unknown(name: str, sent: set[str]) -> str:
    return json.dumps({"error": f"Unknown tool: {name!r}. Available tools: {', '.join(sorted(sent))}"})


@pytest.mark.parametrize("streamed", [False, True], ids=["sequential", "streamed"])
async def test_a_masters_model_cannot_call_a_tool_it_was_not_offered(api, monkeypatch, streamed):
    master = await master_of(api, await create(api))
    refused = {"spawn_task": {}, "delegate_task": {}, "run_coding_agent": {}}
    sent, ran, answered = await turn_calling(monkeypatch, master, refused, streamed=streamed)
    ran.assert_not_awaited()
    assert answered == {f"call_{name}": unknown(name, sent) for name in refused}


@pytest.mark.parametrize("streamed", [False, True], ids=["sequential", "streamed"])
async def test_a_hidden_tool_is_refused_rather_than_repaired_into_another(api, monkeypatch, streamed):
    # write_file is one edit from read_file, which a master has.
    master = await master_of(api, await create(api))
    call = {"write_file": {"path": "notes.md", "content": "Q3 revenue was 4.2M."}}
    sent, ran, answered = await turn_calling(monkeypatch, master, call, streamed=streamed)
    ran.assert_not_awaited()
    assert answered == {"call_write_file": unknown("write_file", sent)}


async def test_a_misspelt_tool_is_still_repaired(api, monkeypatch):
    master = await master_of(api, await create(api))
    _, ran, answered = await turn_calling(monkeypatch, master, {"read_fiel": {"path": "notes.md"}})
    assert [call.args[0] for call in ran.await_args_list] == ["read_file"]
    assert answered == {"call_read_fiel": '{"ok": true}'}


async def test_a_masters_model_makes_a_routine(api, monkeypatch, session_factory):
    master = await master_of(api, await create(api))
    routine = {"cron_create": {"cron": "0 9 * * 1", "prompt": "Check the cash report"}}
    _, _, answered = await turn_calling(monkeypatch, master, routine, session_factory=session_factory)
    assert json.loads(answered["call_cron_create"])["success"] is True
    async with session_factory() as db:
        [schedule] = (await db.scalars(
            select(ScheduledSession).where(ScheduledSession.created_from_session_id == master.id),
        )).all()
    assert (schedule.prompt, schedule.agent_id, schedule.user_id) == ("Check the cash report", AGENT_ID, master.user_id)


@pytest.mark.parametrize("streamed", [False, True], ids=["sequential", "streamed"])
async def test_a_chats_model_calls_its_tools_as_before(api, monkeypatch, streamed):
    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    session = await api.app.state.session_store.get_session(UUID(chat.json()["id"]))
    _, ran, answered = await turn_calling(monkeypatch, session, {"todo": {}, "delegate_task": {}}, streamed=streamed)
    assert [call.args[0] for call in ran.await_args_list] == ["todo", "delegate_task"]
    assert answered == {"call_todo": '{"ok": true}', "call_delegate_task": '{"ok": true}'}


async def system_prompt(api, session) -> str:
    tenant = TenantContext(
        org_id=api.org_id, user_id=api.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    builder = PromptBuilder(tenant, session=session, available_agents=[
        AgentDef(name="analyst", description="Reads the numbers", system_prompt="", source="platform"),
    ])
    return await _make_harness(prompt_builder=builder)._build_system_prompt(session)


async def test_a_masters_prompt_runs_the_project(api):
    project = await create(api, goal="The board's Q3 report", instructions="Use euros.")
    prompt = await system_prompt(api, await master_of(api, project))
    assert "# Running a project" in prompt
    assert "# Worker Delegation" not in prompt
    assert "# Available Sub-Agents" not in prompt
    assert prompt.endswith(
        "## Session instructions\n\nProject: Quarterly report\n\nGoal: The board's Q3 report\n\nUse euros."
    )


async def test_a_coordinator_chat_keeps_its_delegation_prompt(api):
    chat = await api.client.post("/v1/sessions", json={"config": {"coordinator": True}}, headers=api.auth())
    prompt = await system_prompt(api, await api.app.state.session_store.get_session(UUID(chat.json()["id"])))
    assert "# Worker Delegation" in prompt
    assert "# Available Sub-Agents" in prompt
    assert "# Running a project" not in prompt


class Recap:
    """A turn summarizer that always has something to say."""

    async def pick_deliverables(self, *, artifacts, **_):
        return artifacts

    async def summarize_turn(self, **_):
        return TurnSummary(recap="Answered the question.", artifacts=[])


async def turn_summaries(api, session) -> list:
    store = api.app.state.session_store
    lease = await store.try_acquire_lease(session.id, "worker-projects", ttl_seconds=60)
    harness = _make_harness(session_store=store, sandbox_pool=None)
    harness._turn_summarizer = Recap()
    await harness._complete_session(
        session, [{"role": "assistant", "content": "Q3 revenue was 4.2M."}], lease,
        reason="completed", turn_id="turn-1", user_message="What was Q3 revenue?",
    )
    return await store.get_events(session.id, types=[EventType.TURN_SUMMARY])


async def test_a_masters_turn_ends_without_a_recap(api):
    master = await master_of(api, await create(api))
    assert await turn_summaries(api, master) == []
    assert (await api.app.state.session_store.get_session(master.id)).status == "completed"


async def test_a_chats_turn_ends_with_a_recap(api):
    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    session = await api.app.state.session_store.get_session(UUID(chat.json()["id"]))
    [summary] = await turn_summaries(api, session)
    assert summary.data["recap"] == "Answered the question."
