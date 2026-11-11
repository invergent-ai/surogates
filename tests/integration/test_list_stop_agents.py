"""``list_agents`` and ``stop_agent``: what an agent can reach, and stopping what it started."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from surogates.config import INTERRUPT_CHANNEL_PREFIX
from surogates.harness.background_watch import BackgroundWatch, HERE
from surogates.harness import background_watch as watch_module
from surogates.session.events import EventType
from surogates.tools.builtin import agents as agents_module
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.test_steer_loop import _final_response

from .test_agent_tool import PARITY, a_chat, calling, chats, children, tool_result  # noqa: F401  (chats is a fixture)
from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
from .test_send_message import a_background_agent
from .test_workstream_threads import TODO_CALL

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def listed(chats, session) -> dict:  # noqa: F811
    return json.loads(await agents_module._list_agents_handler(
        {}, session_store=chats.store, session_id=str(session), session_factory=chats.api.app.state.session_factory,
    ))


async def stop(chats, session, target) -> dict:  # noqa: F811
    return json.loads(await agents_module._stop_agent_handler(
        {"agent_id": str(target), "reason": "no longer needed"},
        session_store=chats.store, session_id=str(session), redis=chats.api.app.state.redis,
        session_factory=chats.api.app.state.session_factory,
    ))


async def test_an_agent_lists_its_agents_its_parent_and_its_siblings(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    first = await a_background_agent(chats, chat, "First")
    chats.replies = [
        calling("agent", "call_two", description="Second", prompt="Count the closed items.", run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "And the closed ones.")
    await chats.wake(chat)
    second = next(c for c in await children(chats, chat) if c.id != first.id)
    chats.replies = [_final_response("Done.")]
    await chats.wake(second.id)

    mine = await listed(chats, chat)
    assert sorted((a["agent_id"], a["description"], a["status"], a["relation"]) for a in mine["agents"]) == sorted([
        (str(first.id), "First", "running", "yours"), (str(second.id), "Second", "completed", "yours"),
    ])
    theirs = await listed(chats, first.id)
    assert sorted((a["agent_id"], a["relation"]) for a in theirs["agents"]) == sorted([
        (str(chat), "parent"), (str(second.id), "sibling"),
    ])


async def test_stopping_a_running_agent_stops_it_and_tells_its_parent(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    child = await a_background_agent(chats, chat)
    answers: list[dict] = []
    pubsub = chats.api.app.state.redis.pubsub()
    await pubsub.subscribe(f"{INTERRUPT_CHANNEL_PREFIX}:{child.id}")

    async def the_parent_stops_it():
        answers.append(await stop(chats, chat, child.id))
        # The interrupt the stop published reaches the turn under way, as a dispatcher hands it on.
        message = None
        for _ in range(40):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message is not None:
                break
        assert message is not None
        # A fixed word, never the model's reason, which stays in the pause event.
        assert json.loads(message["data"])["reason"] == "stopped by agent"
        chats.harnesses[child.id].interrupt(json.loads(message["data"])["reason"])

    chats.during_the_tool_call = the_parent_stops_it
    chats.replies = [TODO_CALL, _final_response("never asked")]
    await chats.wake(child.id)
    assert answers[0]["status"] == "stopped" and answers[0]["stopped"] == [str(child.id)]
    assert await chats.status(child.id) == "paused"
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["status"] == "killed" and note.data["agent_id"] == str(child.id)
    # The turn stopped at its next step: the model was not asked again.
    said = [e.data["message"]["content"] for e in await chats.store.get_events(child.id, types=[EventType.LLM_RESPONSE])]
    assert "never asked" not in said
    # Stopped, it keeps its context: a message resumes it.
    # The stop's own pause, then the one its turn wrote as it ended.
    pauses = [e.data for e in await chats.store.get_events(child.id, types=[EventType.SESSION_PAUSE])]
    assert pauses[0] == {"reason": "stopped", "message": "no longer needed", "stopped_by": str(chat)}
    assert all(p.get("turn_ended") for p in pauses[1:])
    resumed = json.loads(await agents_module._send_message_handler(
        {"to": str(child.id), "message": "Go on, after all.", "summary": "Go on"},
        session_store=chats.store, session_id=str(chat), redis=chats.api.app.state.redis,
        session_factory=chats.api.app.state.session_factory,
    ))
    assert resumed["status"] == "resumed" and await chats.status(child.id) == "active"
    await pubsub.aclose()


async def test_a_queued_agent_stopped_before_it_runs_is_told_once(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    child = await a_background_agent(chats, chat)
    answer = await stop(chats, chat, child.id)
    assert answer["status"] == "stopped"
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["status"] == "killed"
    # Its queued wake finds it stopped and runs nothing.
    before = len(chats.requests)
    await chats.wake(child.id)
    assert len(chats.requests) == before
    assert (await stop(chats, chat, child.id))["status"] == "stopped"  # not running: nothing to stop
    assert len(await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])) == 1


async def test_an_agent_stops_only_what_it_started(chats):  # noqa: F811
    chat, stranger = await a_chat(chats, PARITY), await a_chat(chats, PARITY)
    child = await a_background_agent(chats, chat, "Mine")
    theirs = await a_background_agent(chats, stranger, "Theirs")
    for sender, target in [(stranger, child.id), (child.id, chat), (theirs.id, child.id), (chat, uuid4()), (chat, theirs.id)]:
        answer = await stop(chats, sender, target)
        assert answer["error"].startswith("no agent "), (sender, target)
    assert await chats.status(child.id) == "active"


async def test_a_background_command_is_listed_and_stopped_by_the_session_that_started_it(api, monkeypatch):  # noqa: F811
    store, redis = api.app.state.session_store, api.app.state.redis
    created = await api.client.post("/v1/sessions", json={"config": PARITY}, headers=api.auth())
    other = await api.client.post("/v1/sessions", json={"config": PARITY}, headers=api.auth())
    mine, theirs = created.json()["id"], other.json()["id"]
    watch = BackgroundWatch(poll_seconds=0.05)
    watch.configure(store=store, redis=redis, sandbox_pool=None)
    monkeypatch.setattr(watch_module, "background_watch", watch)
    started = await LocalWorkspaceIO(workspace_path=None).start(
        "sleep 30", workdir=None, task_id="t", pty=False, notify_on_complete=True, watcher_interval=None,
    )
    proc = started["session_id"]
    from uuid import UUID

    watch.watch(owner=HERE, proc_id=proc, session_id=UUID(mine), command="sleep 30")
    try:
        assert (await listed_api(api, mine))["background_commands"] == [{"id": proc, "command": "sleep 30", "status": "running"}]
        assert (await listed_api(api, theirs))["background_commands"] == []
        refused = json.loads(await agents_module._stop_agent_handler(
            {"agent_id": proc}, session_store=store, session_id=theirs, session_factory=api.app.state.session_factory,
        ))
        assert "no background command" in refused["error"]
        killed = json.loads(await agents_module._stop_agent_handler(
            {"agent_id": proc}, session_store=store, session_id=mine, session_factory=api.app.state.session_factory,
        ))
        assert killed["agent_id"] == proc
        for _ in range(200):
            notes = await store.get_events(UUID(mine), types=[EventType.AGENT_NOTIFICATION])
            if notes:
                break
            await asyncio.sleep(0.05)
        assert notes[0].data["agent_id"] == proc and notes[0].data["status"] == "killed"
    finally:
        await watch.close()


async def listed_api(api, session) -> dict:  # noqa: F811
    return json.loads(await agents_module._list_agents_handler(
        {}, session_store=api.app.state.session_store, session_id=str(session),
        session_factory=api.app.state.session_factory,
    ))


async def test_a_stop_of_a_foreground_agent_ends_the_call_that_waits_for_it(chats):  # noqa: F811
    from surogates.session.provisioning import create_child_session
    from surogates.tools.builtin import delegate

    chat = await a_chat(chats, PARITY)
    child = await create_child_session(store=chats.store, parent=await chats.session(chat), channel="delegation")
    await chats.store.emit_event(child.id, EventType.SESSION_PAUSE, {"reason": "stopped", "message": "enough", "stopped_by": str(chat)})
    outcome = await delegate._poll_child_completion(
        session_store=chats.store, parent_session_id=chat, child_id=child.id, timeout=5, poll_interval=0.05,
    )
    assert outcome == {"status": "failed", "reason": "The agent was stopped: enough"}


async def test_a_stop_reaches_only_agents_of_the_family(chats):  # noqa: F811
    from surogates.harness.subagents import DISPATCH_KEY, PARITY_KEY
    from surogates.session.provisioning import create_child_session

    chat = await a_chat(chats, PARITY)
    # A worker of the chat's, and an agent under it: not the chat's agents.
    worker = await create_child_session(store=chats.store, parent=await chats.session(chat), channel="worker")
    hidden = await create_child_session(
        store=chats.store, parent=worker, channel="delegation",
        config={PARITY_KEY: True, DISPATCH_KEY: {"mode": "background", "description": "Hidden", "parent": str(worker.id)}},
    )
    assert (await stop(chats, chat, hidden.id))["error"].startswith("no agent ")
    assert all(a["agent_id"] != str(hidden.id) for a in (await listed(chats, chat))["agents"])
