"""What a project's sessions share: its memory, its instructions, its files and its routines."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from surogates.harness.prompt import PromptBuilder
from surogates.harness.session_llm import build_session_llm_clients
from surogates.memory.manager import MemoryManager
from surogates.memory.r2_store import R2MemoryStore
from surogates.orchestrator.worker import _build_r2_memory_keys
from surogates.runtime import build_agent_runtime_context
from surogates.scheduled.schedule import parse_schedule
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session.provisioning import create_child_session
from surogates.tenant.context import TenantContext
from tests.test_harness_resilience import _make_harness

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_workstream_threads import start
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


async def model_of(session, *, pro_projected: bool = True) -> str:
    """The model *session*'s worker builds its main slot on, for a basic agent.

    Ops projects the pro endpoint for every hosted basic agent; a BYO agent
    has none.
    """
    ctx = build_agent_runtime_context({
        "agent_id": session.agent_id, "org_id": str(session.org_id), "project_id": "test-project",
        "enabled": True, "version": 1, "storage_key_prefix": PREFIX,
        "llm_main": endpoint("basic"), "llm_tier_pro": endpoint("pro") if pro_projected else None,
    })
    vault = SimpleNamespace(resolve_ref=AsyncMock(return_value="sk-test"))
    bundle = await build_session_llm_clients(ctx, vault=vault, user_id=session.user_id, session_config=session.config)
    try:
        return bundle.main.model
    finally:
        await bundle.aclose()


async def test_the_coordinator_and_the_threads_run_on_their_projects_tiers(api):
    project = await create(api)
    await patch(api, project, {"coordinator_tier": "pro"})
    master = await master_of(api, project)
    plain = await start(api, master)
    await patch(api, project, {"coordinator_tier": None, "thread_tier": "pro"})
    pro = await start(api, await master_of(api, project), title="Summarise B", goal="Summarise B.pdf.")
    assert [await model_of(s) for s in (master, plain, pro, await master_of(api, project))] == [
        "pro-model", "basic-model", "pro-model", "basic-model",
    ]
    # A BYO agent has no other tier's endpoint, so a project's tier changes nothing.
    assert await model_of(pro, pro_projected=False) == "basic-model"


async def test_the_users_package_tier_wins_over_the_projects(api):
    project = await create(api)
    await patch(api, project, {"thread_tier": "pro"})
    master = await master_of(api, project)
    # The master's last message pinned the user's package, which keeps them on basic.
    await api.app.state.session_store.reconcile_session_config_key(master.id, "entitlements", {"model_tier": "basic"})
    thread = await start(api, await master_of(api, project))
    assert thread.config["entitlements"] == {"model_tier": "basic"}
    assert await model_of(thread) == "basic-model"


async def test_a_threads_helper_runs_under_the_users_package(api):
    project = await create(api)
    store = api.app.state.session_store
    package = {"model_tier": "basic", "capabilities": ["code"]}
    await store.reconcile_session_config_key((await master_of(api, project)).id, "entitlements", package)
    thread = await start(api, await master_of(api, project))
    # A helper's turn, often its only one, runs before any hold of its own pins the package.
    helper = await create_child_session(store=store, parent=thread, channel="delegation")
    assert helper.config["entitlements"] == package


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
