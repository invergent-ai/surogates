"""A project's master gives out its threads with the ``agent`` family.

``agent`` starts a thread (or proposes one), ``send_message`` follows one up,
``stop_agent`` stops or resolves one, and ``list_agents`` lists them or reads
one.  The thread's machinery (its session, row, landing and reports) is the
project's own, as before.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.db.models import WorkstreamThread
from surogates.session.events import EventType
from surogates.workstreams.threads import begin_thread, make_thread

from .test_devices import api  # noqa: F401  (a fixture)
from .test_projects_on_a_computer import held, on_computer
from .test_workstream_threads import call_tool, children_of, events_of
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def a_thread(api, master, title="Draft A", goal="Draft the A memo as A.docx."):
    started = await call_tool(api, master, "agent", description=title, prompt=goal)
    assert started.get("status") == "running", started
    return await api.app.state.session_store.get_session(UUID(started["agent_id"]))


async def test_a_master_starts_a_thread_with_agent(api):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, master)
    assert (thread.parent_id, thread.channel, thread.config["workstream_role"]) == (master.id, "worker", "thread")
    [spawned] = await events_of(api, master.id, EventType.WORKER_SPAWNED)
    assert spawned.data["worker_id"] == str(thread.id) and spawned.data["title"] == "Draft A"
    async with api.app.state.session_factory() as db:
        assert (await db.get(WorkstreamThread, thread.id)).title == "Draft A"


async def test_a_master_proposes_a_thread_with_agent(api):
    master = await master_of(api, await create(api))
    proposed = await call_tool(
        api, master, "agent", description="Check the totals", prompt="Check the totals in Budget.xlsx.",
        propose=True, where="cloud",
    )
    assert proposed["status"] == "proposed", proposed
    [event] = await events_of(api, master.id, EventType.THREAD_PROPOSED)
    assert event.data["threads"] == [
        {"key": "1", "title": "Check the totals", "goal": "Check the totals in Budget.xlsx.", "where": "cloud"},
    ]
    assert await children_of(api, master) == []


async def test_a_master_messages_stops_and_resolves_a_thread(api):
    store = api.app.state.session_store
    master = await master_of(api, await create(api))
    thread = await a_thread(api, master)
    sent = await call_tool(api, master, "send_message", to=str(thread.id), message="Use euros.", summary="Currency")
    assert sent["status"] == "delivered", sent
    stopped = await call_tool(api, master, "stop_agent", agent_id=str(thread.id))
    assert stopped["status"] == "stopped", stopped
    assert (await store.get_session(thread.id)).status == "paused"
    resolved = await call_tool(api, master, "stop_agent", agent_id=str(thread.id), resolve=True)
    assert resolved["status"] == "resolved", resolved
    async with api.app.state.session_factory() as db:
        assert (await db.get(WorkstreamThread, thread.id)).resolved_at is not None


async def test_a_master_lists_and_reads_its_threads(api):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, master)
    listed = await call_tool(api, master, "list_agents")
    assert [(a["agent_id"], a["description"]) for a in listed["agents"]] == [(str(thread.id), "Draft A")]
    assert {"group", "reason", "status_line"} <= set(listed["agents"][0])
    read = await call_tool(api, master, "list_agents", agent_id=str(thread.id))
    assert read["agent_id"] == str(thread.id) and read["description"] == "Draft A"
    assert {"report", "question"} <= set(read)


async def test_a_master_on_a_computer_must_propose(api):
    project, _ = await on_computer(api)
    master = await master_of(api, project)
    refused = await held(api, master, "agent", description="Draft A", prompt="Draft the A memo as A.docx.")
    assert "propose" in refused["error"] and 'where: "device"' in refused["error"], refused
    assert await children_of(api, master) == []
    proposed = await held(api, master, "agent", description="Draft A", prompt="Draft the A memo.", propose=True,
                          where="device")
    assert proposed["status"] == "proposed", proposed


async def test_a_masters_thread_takes_no_fork_type_or_name(api):
    import json
    from types import SimpleNamespace

    from surogates.tools.builtin.agents import _agent_handler

    master = await master_of(api, await create(api))
    for extra in ({"fork": True}, {"agent_type": "analyst"}):
        refused = await call_tool(api, master, "agent", description="Draft A", prompt="Draft A.", **extra)
        assert "brief" in refused["error"], (extra, refused)
    # ``name`` is call_tool's own parameter: the handler is called directly.
    refused = json.loads(await _agent_handler(
        {"description": "Draft A", "prompt": "Draft A.", "name": "drafter"},
        session_store=api.app.state.session_store, session_factory=api.app.state.session_factory,
        tenant=SimpleNamespace(org_id=master.org_id), session_id=str(master.id),
    ))
    assert "brief" in refused["error"], refused
    assert await children_of(api, master) == []


async def test_propose_and_resolve_are_refused_outside_a_project(api):
    chat_id = UUID((await api.client.post("/v1/sessions", json={}, headers=api.auth())).json()["id"])
    chat = await api.app.state.session_store.get_session(chat_id)
    for name, arguments in (
        ("agent", {"description": "A", "prompt": "Do A.", "propose": True, "where": "cloud"}),
        ("stop_agent", {"agent_id": "a1", "resolve": True}),
    ):
        refused = await call_tool(api, chat, name, **arguments)
        assert "only a project's master" in refused["error"], (name, refused)


async def test_a_master_reaches_a_thread_started_before_this_change(api):
    """A thread made without the family's tools, as the two on PROD were: the master's family reaches it."""
    store = api.app.state.session_store
    master = await master_of(api, await create(api))
    thread = await make_thread(
        session_store=store, session_factory=api.app.state.session_factory, master=master, live_config=None,
        title="Old thread",
    )
    await begin_thread(
        session_store=store, session_factory=api.app.state.session_factory, redis=None, master=master,
        thread=thread, title="Old thread", goal="Do the old work.", context="",
    )
    listed = await call_tool(api, master, "list_agents")
    assert str(thread.id) in [a["agent_id"] for a in listed["agents"]]
    sent = await call_tool(api, master, "send_message", to=str(thread.id), message="Carry on.", summary="Go on")
    assert "error" not in sent, sent


async def test_a_masters_schemas_have_propose_and_resolve_and_a_chats_do_not():
    from surogates.harness.tool_schemas import describe_for_project
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.runtime import ToolRuntime

    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    schemas = registry.get_schemas(names={"agent", "stop_agent"})

    def properties(config):
        return {
            s["function"]["name"]: set(s["function"]["parameters"]["properties"])
            for s in describe_for_project(schemas, config)
        }

    master = properties({"workstream_role": "coordinator", "workstream_id": "p1"})
    chat = properties({})
    assert {"propose", "where"} <= master["agent"] and "resolve" in master["stop_agent"]
    assert not {"propose", "where"} & chat["agent"] and "resolve" not in chat["stop_agent"]
