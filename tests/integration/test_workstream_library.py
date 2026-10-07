"""What a project's sessions share: its memory, its instructions, its files and its routines."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import surogates.api.routes.workspace as workspace_routes
import surogates.harness.slash_skill as slash_skill
from surogates.harness.prompt import PromptBuilder
from surogates.harness.session_llm import build_session_llm_clients
from surogates.harness.slash_skill import build_expanded_message
from surogates.memory.manager import MemoryManager
from surogates.memory.r2_store import R2MemoryStore
from surogates.orchestrator.worker import _build_r2_memory_keys
from surogates.runtime import build_agent_runtime_context
from surogates.scheduled.schedule import parse_schedule
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.storage.tenant import boundary_workspace_key
from surogates.tenant.context import TenantContext
from surogates.workstreams.derive import SHELL_LIMITS
from tests.test_harness_resilience import _make_harness

from .test_devices import add_user, api  # noqa: F401  (api is a fixture)
from .test_workstream_threads import answered, start, turn_ends, turn_of_the_master_ends, waking
from .test_workstreams import create, master_of, patch, runtime, system_prompt, turn_calling

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Memory keys sit under the agent's storage prefix, which is never empty.
PREFIX = "agents/q3"


@pytest.fixture(autouse=True)
def _prefixed(api):
    runtime(api, storage_key_prefix=PREFIX)


async def remember(api, note: str, session=None) -> None:
    """What the memory tool does in *session*: the harness's client posts the
    note with the session's id.  Without one it is the user's own memory, as
    the memory settings write it."""
    query = "" if session is None else f"?session_id={session.id}"
    response = await api.client.post(
        f"/v1/memory{query}", json={"action": "add", "target": "memory", "content": note}, headers=api.auth(),
    )
    assert response.status_code == 200 and response.json()["success"], response.text


async def memory_listed(api, session=None) -> list[str]:
    query = "" if session is None else f"?session_id={session.id}"
    response = await api.client.get(f"/v1/memory{query}", headers=api.auth())
    assert response.status_code == 200, response.text
    return response.json()["memory"]


async def prompt_of(api, session) -> str:
    """*session*'s system prompt, with the memory its worker loads at a wake."""
    settings = api.app.state.settings
    store = R2MemoryStore(
        backend=api.app.state.storage,
        bucket=settings.storage.memory_bucket or settings.storage.bucket,
        keys=_build_r2_memory_keys(session=session, storage_key_prefix=PREFIX, user_id=str(session.user_id)),
    )
    await store.load_from_r2()
    tenant = TenantContext(
        org_id=api.org_id, user_id=api.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    builder = PromptBuilder(tenant, session=session, memory_manager=MemoryManager(store))
    return await _make_harness(prompt_builder=builder)._build_system_prompt(session)


async def test_what_one_thread_remembers_is_in_the_next_threads_prompt(api):
    project = await create(api)
    master = await master_of(api, project)
    first = await start(api, master)
    await remember(api, "The board reads every figure in euros.", first)

    second = await start(api, master, title="Summarise B", goal="Summarise B.pdf.")
    assert "The board reads every figure in euros." in await prompt_of(api, second)
    # The project's Memory settings read it through the master.
    assert await memory_listed(api, master) == ["The board reads every figure in euros."]
    assert await memory_listed(api) == []


async def test_a_project_leaves_the_users_own_memory_out(api):
    await remember(api, "Flavius likes short answers.")
    master = await master_of(api, await create(api))
    thread = await start(api, master)
    assert "Flavius likes short answers." not in await prompt_of(api, master)
    assert "Flavius likes short answers." not in await prompt_of(api, thread)

    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    plain = await api.app.state.session_store.get_session(UUID(chat.json()["id"]))
    assert "Flavius likes short answers." in await prompt_of(api, plain)


async def test_one_projects_memory_is_not_anothers(api):
    first = await master_of(api, await create(api))
    await remember(api, "Use the Q3 template.", first)
    other = await master_of(api, await create(api, name="Budget"))
    assert "Use the Q3 template." not in await prompt_of(api, await start(api, other))
    assert await memory_listed(api, other) == []


async def test_a_chat_cannot_name_a_projects_memory(api):
    master = await master_of(api, await create(api))
    await remember(api, "Use the Q3 template.", master)
    boundary = master.config["memory_boundary"]
    chat = await api.client.post("/v1/sessions", json={"config": {
        "memory_boundary": boundary, "workspace_boundary": boundary,
    }}, headers=api.auth())
    assert chat.status_code == 201, chat.text
    assert not {"memory_boundary", "workspace_boundary"} & set(chat.json()["config"])
    plain = await api.app.state.session_store.get_session(UUID(chat.json()["id"]))
    assert "Use the Q3 template." not in await prompt_of(api, plain)
    assert await memory_listed(api, plain) == []


async def test_a_thread_starts_with_the_projects_name_and_instructions(api):
    project = await create(api, goal="The board's Q3 report", instructions="Use euros.")
    thread = await start(api, await master_of(api, project))
    # The thread's own goal is its first message; the project's goal is the master's.
    assert thread.config["system"] == "Project: Quarterly report\nThread: Draft A\nFolder: threads/Draft A/\n\nUse euros."
    prompt = await system_prompt(api, thread)
    assert prompt.endswith("## Session instructions\n\n" + thread.config["system"])
    assert "Save the files you produce in the folder of the `Folder:` line of your" in prompt


async def test_a_change_to_the_instructions_reaches_new_threads_and_the_master(api):
    project = await create(api, instructions="Use euros.")
    master = await master_of(api, project)
    running = await start(api, master)
    response = await patch(api, project, {"name": "Annual report", "instructions": "Use dollars."})
    assert response.status_code == 200, response.text

    assert (await master_of(api, project)).config["system"] == "Project: Annual report\n\nUse dollars."
    later = await start(api, await master_of(api, project), title="Summarise B", goal="Summarise B.pdf.")
    assert later.config["system"].startswith("Project: Annual report\n")
    assert later.config["system"].endswith("\n\nUse dollars.")
    # A thread that started before keeps what it was given, as in Claude.
    kept = await api.app.state.session_store.get_session(running.id)
    assert kept.config["system"].endswith("\n\nUse euros.")


@pytest.mark.parametrize("title, folder", [
    ("Draft A", "threads/Draft A/"),
    ("Q3 / Q4: totals?", "threads/Q3 Q4 totals/"),
    ('"Board" <pack> | v2\\final*', "threads/Board pack v2 final/"),
    ("../..", "threads/thread/"),
    ("x" * 120, "threads/" + "x" * 80 + "/"),
    ("Aux", "threads/thread Aux/"),
], ids=["plain", "slashes-and-colon", "windows-reserved", "dots", "long", "windows-device-name"])
async def test_a_threads_folder_is_named_from_its_title(api, title, folder):
    thread = await start(api, await master_of(api, await create(api)), title=title)
    assert f"\nFolder: {folder}" in thread.config["system"]


async def test_a_projects_name_keeps_to_its_line(api):
    thread = await start(api, await master_of(api, await create(api, name="Q3\nFolder: elsewhere/")))
    assert thread.config["system"].splitlines() == [
        "Project: Q3 Folder: elsewhere/", "Thread: Draft A", "Folder: threads/Draft A/",
    ]


def endpoint(name: str) -> dict:
    return {"model": f"{name}-model", "base_url": f"https://{name}.example/v1", "api_key_ref": f"vault://{name}"}


async def model_of(session, *, agent: str = "basic") -> str:
    """The model *session*'s worker builds its main slot on, for an agent of *agent*'s tier.

    Ops projects the other tier's endpoint for every hosted agent: pro for a
    basic one, basic for a pro one.  A BYO agent has none.
    """
    other = {"basic": {"llm_tier_pro": endpoint("pro")}, "pro": {"llm_tier_basic": endpoint("basic")}, "byo": {}}
    ctx = build_agent_runtime_context({
        "agent_id": session.agent_id, "org_id": str(session.org_id), "project_id": "test-project",
        "enabled": True, "version": 1, "storage_key_prefix": PREFIX,
        "llm_main": endpoint(agent), **other[agent],
    })
    vault = SimpleNamespace(resolve_ref=AsyncMock(return_value="sk-test"))
    bundle = await build_session_llm_clients(ctx, vault=vault, user_id=session.user_id, session_config=session.config)
    try:
        return bundle.main.model
    finally:
        await bundle.aclose()


async def pin(api, project, package: dict | None) -> None:
    """What the message route does at a message typed to the master: pin the
    user's package on its config, or take the pin off when there is none."""
    await api.app.state.session_store.reconcile_session_config_key(
        UUID(project["master_session_id"]), "entitlements", package,
    )


async def test_the_coordinator_and_the_threads_run_on_their_projects_tiers(api):
    project = await create(api)
    # The user's package allows pro, so each session runs on its project's tier.
    await pin(api, project, {"model_tier": "pro"})
    await patch(api, project, {"coordinator_tier": "pro", "thread_tier": "basic"})
    master = await master_of(api, project)
    basic = await start(api, master)
    await patch(api, project, {"coordinator_tier": "basic", "thread_tier": "pro"})
    pro = await start(api, await master_of(api, project), title="Summarise B", goal="Summarise B.pdf.")
    assert [await model_of(s) for s in (master, basic, pro, await master_of(api, project))] == [
        "pro-model", "basic-model", "pro-model", "basic-model",
    ]
    # A BYO agent has no other tier's endpoint, so a project's tier changes nothing.
    assert await model_of(pro, agent="byo") == "byo-model"


@pytest.mark.parametrize("package", [None, {"capabilities": ["code"]}], ids=["no-package", "no-tier"])
async def test_a_project_runs_no_higher_than_the_users_package_allows(api, package):
    # A package that names no tier allows the agent's own, as no package does.
    project = await create(api)
    await pin(api, project, package)
    await patch(api, project, {"coordinator_tier": "pro", "thread_tier": "pro"})
    master = await master_of(api, project)
    assert [await model_of(s) for s in (master, await start(api, master))] == ["basic-model", "basic-model"]


@pytest.mark.parametrize("package", [None, {"model_tier": "pro"}], ids=["no-package", "pro-package"])
async def test_a_project_on_basic_runs_a_pro_agent_on_basic(api, package):
    project = await create(api)
    await pin(api, project, package)
    await patch(api, project, {"coordinator_tier": "basic", "thread_tier": "basic"})
    master = await master_of(api, project)
    assert [await model_of(s, agent="pro") for s in (master, await start(api, master))] == [
        "basic-model", "basic-model",
    ]


async def test_the_users_package_tier_wins_over_the_projects(api):
    project = await create(api)
    await patch(api, project, {"thread_tier": "pro"})
    # The master's last message pinned the user's package, which keeps them on basic.
    await pin(api, project, {"model_tier": "basic"})
    thread = await start(api, await master_of(api, project))
    assert thread.config["entitlements"] == {"model_tier": "basic"}
    assert await model_of(thread) == "basic-model"


async def test_a_threads_helper_runs_under_the_users_package(api):
    project = await create(api)
    package = {"model_tier": "basic", "capabilities": ["code"]}
    await pin(api, project, package)
    thread = await start(api, await master_of(api, project))
    # A helper's turn, often its only one, runs before any hold of its own pins the package.
    helper = await create_child_session(store=api.app.state.session_store, parent=thread, channel="delegation")
    assert helper.config["entitlements"] == package


async def test_a_child_takes_its_parents_package_and_no_other(api):
    thread = await start(api, await master_of(api, await create(api)))
    helper = await create_child_session(
        store=api.app.state.session_store, parent=thread, channel="delegation",
        config={"entitlements": {"model_tier": "pro"}},
    )
    assert "entitlements" not in helper.config


async def routine_made_in(monkeypatch, session, session_factory, prompt: str) -> None:
    """*session*'s model makes a routine with ``cron_create``."""
    call = {"cron_create": {"cron": "0 9 * * 1", "prompt": prompt, "name": prompt}}
    _, _, answered = await turn_calling(monkeypatch, session, call, session_factory=session_factory)
    assert json.loads(answered["call_cron_create"])["success"] is True


async def test_the_routines_are_the_schedules_the_master_made(api, monkeypatch, session_factory):
    master = await master_of(api, await create(api))
    await routine_made_in(monkeypatch, master, session_factory, "Check the cash report")
    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    plain = await api.app.state.session_store.get_session(UUID(chat.json()["id"]))
    # An ordinary chat makes its routines with a typed /loop.
    await ScheduledSessionStore(session_factory).create(
        org_id=plain.org_id, user_id=plain.user_id, agent_id=plain.agent_id, name="Water the plants",
        prompt="Water the plants", schedule=parse_schedule("0 9 * * 1"), source="loop",
        created_from_session_id=plain.id,
    )

    response = await api.client.get(f"/v1/scheduled-work?created_from_session_id={master.id}", headers=api.auth())
    assert response.status_code == 200, response.text
    [routine] = response.json()["items"]
    assert (routine["name"], routine["schedule_display"], routine["status"]) == (
        "Check the cash report", "0 9 * * 1", "active",
    )
    everything = await api.client.get("/v1/scheduled-work", headers=api.auth())
    assert len(everything.json()["items"]) == 2


async def test_a_routines_name_is_kept_to_what_the_shell_shows(api, monkeypatch, session_factory):
    master = await master_of(api, await create(api))
    await routine_made_in(monkeypatch, master, session_factory, "Check the cash report " + "and the ledger " * 40)
    response = await api.client.get(f"/v1/scheduled-work?created_from_session_id={master.id}", headers=api.auth())
    [routine] = response.json()["items"]
    assert routine["name"] == ("Check the cash report " + "and the ledger " * 40)[:200]


async def upload(api, master, name: str, data: bytes, directory: str = "") -> None:
    """The user adds a file to the project, as the web client uploads one to its master."""
    query = f"?path={directory}" if directory else ""
    response = await api.client.post(
        f"/v1/sessions/{master.id}/workspace/upload{query}", files={"file": (name, data)}, headers=api.auth(),
    )
    assert response.status_code == 201, response.text


async def written(api, session, path: str, data: bytes = b"x", *, modified: float | None = None) -> None:
    """*session*'s pod writes *path* into the project's workspace, last
    changed at *modified* (a POSIX time) when given."""
    storage, bucket = api.app.state.storage, session.config["storage_bucket"]
    key = boundary_workspace_key(session.config, session, str(session.id), path)
    await storage.write(bucket, key, data)
    if modified is not None:  # the tests' storage is a local disk
        os.utime(storage._resolve(bucket, key), (modified, modified))


async def library(api, project, token=None):
    return await api.client.get(f"/v1/workstreams/{project['id']}/library", headers=api.auth(token))


async def test_the_library_shows_an_upload_as_added_and_a_threads_file_as_produced(api):
    project = await create(api)
    master = await master_of(api, project)
    await upload(api, master, "brief.pdf", b"%PDF-1.7 brief")
    await upload(api, master, "notes.txt", b"call the auditors", directory="uploads")
    thread = await start(api, master)
    await written(api, thread, "threads/Draft A/A.docx", b"PK memo")
    await turn_ends(api, thread, files=["threads/Draft A/A.docx"])

    response = await library(api, project)
    assert response.status_code == 200, response.text
    entries = {entry["path"]: entry for entry in response.json()}
    assert {path: (e["origin"], e["thread_id"], e["size"], e["place"]) for path, e in entries.items()} == {
        "threads/Draft A/A.docx": ("produced", str(thread.id), 7, {"kind": "cloud"}),
        "brief.pdf": ("added", None, 14, {"kind": "cloud"}),
        "uploads/notes.txt": ("added", None, 17, {"kind": "cloud"}),
    }
    assert all(e["updated_at"].endswith("Z") for e in entries.values())
    # An entry opens through the master's file route, over the same files.
    opened = await api.client.get(
        f"/v1/sessions/{master.id}/workspace/download", params={"path": "threads/Draft A/A.docx"}, headers=api.auth(),
    )
    assert (opened.status_code, opened.content) == (200, b"PK memo")


async def test_a_file_two_threads_produced_is_the_last_ones(api):
    project = await create(api)
    master = await master_of(api, project)
    first, second = await start(api, master), await start(api, master, title="Check A", goal="Check A.docx.")
    await written(api, first, "A.docx")
    # Last by event id, not the thread started last; a ref as the model gave it.
    await turn_ends(api, second, files=["A.docx"])
    await turn_ends(api, first, files=["./A.docx"])
    [entry] = (await library(api, project)).json()
    assert (entry["origin"], entry["thread_id"]) == ("produced", str(first.id))


async def test_the_library_and_the_file_panel_leave_out_the_platforms_own_files(api, monkeypatch):
    project = await create(api)
    master = await master_of(api, project)
    thread = await start(api, master)
    await upload(api, master, "brief.pdf", b"brief")
    # A coding tool's checkout, an artifact's payload, the whiteboard, bytecode.
    for path in (
        f".threads/{thread.id}/reports/README.md", f".threads/{thread.id}/reports/totals.py",
        "_artifacts/a1/meta.json", "_whiteboard/canvas.json", "threads/Draft A/__pycache__/totals.cpython-312.pyc",
    ):
        await written(api, thread, path)
    assert [e["path"] for e in (await library(api, project)).json()] == ["brief.pdf"]

    # A thread's checkouts do not count against the panel's limit.
    monkeypatch.setattr(workspace_routes, "_MAX_ENTRIES", 3)
    tree = await api.client.get(f"/v1/sessions/{master.id}/workspace/tree", headers=api.auth())
    assert tree.status_code == 200, tree.text
    assert ([e["path"] for e in tree.json()["entries"]], tree.json()["truncated"]) == (["threads", "brief.pdf"], False)


async def test_a_threads_dependencies_do_not_push_the_users_files_out_of_the_library(api):
    project = await create(api)
    master = await master_of(api, project)
    thread = await start(api, master)
    await written(api, master, "brief.pdf", modified=1_790_000_000)
    # An npm install in the thread's folder: more files than the shell takes, all newer.
    for n in range(SHELL_LIMITS["library"] + 1):
        await written(api, thread, f"threads/Draft A/node_modules/pkg{n}/index.js")
    await written(api, thread, "threads/Draft A/venv/lib/site.py")
    await written(api, thread, "threads/Draft A/A.docx")
    # The Library leaves out what the file panel skips.
    assert [e["path"] for e in (await library(api, project)).json()] == ["threads/Draft A/A.docx", "brief.pdf"]


async def test_the_library_lists_the_newest_files_the_shell_takes(api, monkeypatch):
    project = await create(api)
    master = await master_of(api, project)
    # Newest first, within one second too: a whole second is older than half past it.
    for name, modified in (("old.txt", 1_790_000_000), ("middle.txt", 1_790_000_001), ("new.txt", 1_790_000_001.5)):
        await written(api, master, name, modified=modified)
    # A path longer than the shell takes (4,096 units; 40 here) is left out.
    await written(api, master, "reports/" + "a" * 40 + ".txt")
    monkeypatch.setitem(SHELL_LIMITS, "ref", 40)
    monkeypatch.setitem(SHELL_LIMITS, "library", 2)
    assert [e["path"] for e in (await library(api, project)).json()] == ["new.txt", "middle.txt"]


async def test_the_library_is_its_owners(api, session_factory):
    project = await create(api)
    _, other = await add_user(session_factory, api.org_id)
    assert (await library(api, project, other)).status_code == 404


BOARD_PACK = "Lay the pack out as the board likes it: one page per figure."


async def reported_after(api, text: str, ran: tuple[EventType, dict] | None):
    """A master whose last message was *text*, which its wake ran as *ran*
    (an event and its data), and whose thread has reported since."""
    master = await master_of(api, await create(api))
    thread = await start(api, master)
    store = api.app.state.session_store
    await store.emit_event(master.id, EventType.USER_MESSAGE, {"content": text})
    if ran is not None:
        await store.emit_event(master.id, *ran)
    await turn_of_the_master_ends(api, master)
    await answered(api, thread, "Drafted the memo.")
    await turn_ends(api, thread)
    return master


def skills_answering(harness):
    harness._tools.dispatch = AsyncMock(return_value=json.dumps({"success": True, "content": BOARD_PACK}))
    return harness._tools.dispatch


async def test_a_report_wake_reads_the_skill_the_users_last_message_ran(api, monkeypatch):
    invoked = {"skill": "board-pack", "raw_message": "/board-pack Q3", "staged_at": None}
    master = await reported_after(api, "/board-pack Q3", (EventType.SKILL_INVOKED, invoked))
    # An expert of that name, made since, is not consulted: only the skill's path runs again.
    expert = SimpleNamespace(name="board-pack", is_active_expert=True)
    monkeypatch.setattr(slash_skill, "_load_skills_for_slash", AsyncMock(return_value=[expert]))
    monkeypatch.setattr(slash_skill, "_expand_expert", consulted := AsyncMock(return_value=None))
    harness, handed = waking(api, monkeypatch)
    skill_view = skills_answering(harness)
    await harness.wake(master.id)
    consulted.assert_not_awaited()
    [conversation] = handed
    expanded = build_expanded_message(name="board-pack", args="Q3", skill_body=BOARD_PACK)
    # As its own wake sent it, so the prompt cache still holds the conversation.
    assert {"role": "user", "content": expanded} in conversation
    assert {"role": "user", "content": "/board-pack Q3"} not in conversation
    assert [call.args[:2] for call in skill_view.await_args_list] == [("skill_view", {"name": "board-pack"})]
    assert len(await api.app.state.session_store.get_events(master.id, types=[EventType.SKILL_INVOKED])) == 1


@pytest.mark.parametrize("text, ran", [
    ("/cfo Check the Q3 margins", (EventType.EXPERT_DELEGATION, {"expert": "cfo"})),
    ("Draft the Q3 pack", None),
], ids=["expert", "plain"])
async def test_a_report_wake_runs_nothing_the_users_last_message_asked_for(api, monkeypatch, text, ran):
    master = await reported_after(api, text, ran)
    harness, handed = waking(api, monkeypatch)
    dispatch = skills_answering(harness)
    await harness.wake(master.id)
    dispatch.assert_not_awaited()
    assert {"role": "user", "content": text} in handed[0]
