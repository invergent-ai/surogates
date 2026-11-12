"""The ``agent`` tool: a sub-agent the model starts on its own, in the foreground or the background, fresh or forked.

The chats' turns run the real harness against a scripted model; the
``agent`` call runs for real, and so do the sub-agents' turns, each woken as a
worker wakes the session it took off the queue.
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import pytest
import pytest_asyncio

import surogates.harness.loop as loop_module
from surogates.harness.agent_news import NEW_FORMAT, notification_note
from surogates.harness.subagents import COMPANION_TOOLS, DISPATCH_KEY, PARITY_KEY, PARITY_TOOLS
from surogates.session.events import EventType
from surogates.tools.builtin import agents as agents_module
from surogates.tools.builtin import delegate as delegate_module
from tests.test_steer_loop import _final_response

from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
from .test_workstream_threads import queued

pytestmark = pytest.mark.asyncio(loop_scope="session")

PARITY = {PARITY_KEY: True}
#: The family's tools run for real; every other tool answers as the chats' stub does.
REAL = frozenset({"agent", "send_message", "list_agents", "stop_agent"})


@pytest_asyncio.fixture(loop_scope="session")
async def chats(workers, monkeypatch):  # noqa: F811
    """The chats' workers, with the sub-agent tools run for real, and the tools each request offered."""
    monkeypatch.setattr(delegate_module, "_POLL_INTERVAL_SECONDS", 0.05)
    workers.offered = []
    build = workers.worker

    def worker(*args, **kwargs):
        harness = build(*args, **kwargs)
        stub, real = harness._tools.dispatch, type(harness._tools).dispatch.__get__(harness._tools)

        async def dispatch(name, arguments, **kw):
            return await (real if name in REAL else stub)(name, arguments, **kw)

        harness._tools.dispatch = dispatch
        wake = harness.wake

        async def recorded(session_id):
            # The harness that runs each session's turn, as a dispatcher keeps it to hand it interrupts.
            workers.harnesses[session_id] = harness
            return await wake(session_id)

        harness.wake = recorded
        return harness

    workers.harnesses = {}
    workers.worker = worker
    model = loop_module.call_llm_with_retry

    async def offered(**kwargs):
        workers.offered.append([t["function"]["name"] for t in kwargs["create_kwargs"].get("tools") or []])
        return await model(**kwargs)

    monkeypatch.setattr(loop_module, "call_llm_with_retry", offered)
    return workers


async def a_chat(chats, config: dict | None = None) -> UUID:
    """A chat of the app's user whose config holds *config*: the family's keys are the server's to write (a
    client's are dropped), so they are written as the deployment's setting would have them."""
    created = await chats.api.client.post("/v1/sessions", json={}, headers=chats.api.auth())
    assert created.status_code == 201, created.text
    chat = UUID(created.json()["id"])
    for key, value in (config or {}).items():
        await chats.store.update_session_config_key(chat, key, value)
    return chat


def calling(name: str, call_id: str, **arguments) -> tuple[dict, dict]:
    return (
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )


async def children(chats, chat: UUID) -> list:
    async with chats.api.app.state.session_factory() as db:
        from sqlalchemy import select

        from surogates.db.models import Session as SessionRow

        rows = (await db.execute(select(SessionRow).where(SessionRow.parent_id == chat))).scalars().all()
    return [await chats.store.get_session(row.id) for row in rows]


async def tool_result(chats, chat: UUID, call_id: str) -> str:
    results = await chats.store.get_events(chat, types=[EventType.TOOL_RESULT])
    return next(e.data["content"] for e in results if e.data.get("tool_call_id") == call_id)


async def test_parity_off_offers_none_of_the_family_and_on_offers_agent_in_delegate_tasks_place(chats):
    off, on = await a_chat(chats), await a_chat(chats, PARITY)
    for chat in (off, on):
        await chats.says(chat, "Hello.")
        await chats.wake(chat)
    off_tools, on_tools = chats.offered[-2], chats.offered[-1]
    assert "delegate_task" in off_tools and "agent" not in off_tools
    assert "agent" in on_tools and "delegate_task" not in on_tools
    assert COMPANION_TOOLS <= set(on_tools)
    # Everything else is offered alike, in the same order.
    assert [t for t in off_tools if t != "delegate_task"] == [t for t in on_tools if t not in PARITY_TOOLS]


async def test_a_background_agent_returns_its_id_at_once_and_its_report_wakes_its_parent(chats):
    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", "call_agent", description="Count open items", prompt="Count the open items in the tracker.",
                run_in_background=True),
        _final_response("An agent is counting them."),
    ]
    await chats.says(chat, "How many open items are there?")
    await chats.wake(chat)

    started = json.loads(await tool_result(chats, chat, "call_agent"))
    [child] = await children(chats, chat)
    assert started["agent_id"] == str(child.id) and started["status"] == "running"
    assert child.channel == "delegation" and child.config[PARITY_KEY] is True
    assert child.config[DISPATCH_KEY] == {
        "description": "Count open items", "mode": "background", "fork": False,
        "tool_call_id": "call_agent", "parent": str(chat),
    }
    assert await chats.status(chat) == "completed"
    assert await queued(chats.api, child)

    chats.replies = [_final_response("There are 12 open items.")]
    await chats.wake(child.id)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert {k: v for k, v in note.data.items() if k not in ("usage", "format")} == {
        "agent_id": str(child.id), "kind": "agent", "status": "completed", "summary": "Count open items",
        "result": "There are 12 open items.", "tool_call_id": "call_agent",
    }
    assert note.data["format"] == NEW_FORMAT
    usage = note.data["usage"]
    assert set(usage) == {"subagent_tokens", "tool_uses", "duration_ms"}
    assert all(isinstance(value, int) for value in usage.values()) and usage["duration_ms"] >= 0
    assert await queued(chats.api, await chats.session(chat))

    before = len(chats.requests)
    chats.replies = [_final_response("There are 12.")]
    await chats.wake(chat)
    request = chats.requests[before]
    assert request[-1] == notification_note(note.data)
    assert f'<summary>Agent {child.id} ("Count open items") finished</summary>' in request[-1]["content"]
    await chats.says(chat, "Thanks.")
    await chats.wake(chat)
    assert chats.requests[-1][: len(request)] == request


async def wake_children_as_they_come(chats, chat: UUID, replies: list, skip=frozenset()) -> asyncio.Task:
    """Wake each sub-agent of *chat* once it is created, answering its first request with the next of *replies*;
    never one whose id is in *skip*."""
    async def run():
        woken: set[UUID] = set(skip)
        while True:
            for child in await children(chats, chat):
                if child.id not in woken:
                    woken.add(child.id)
                    chats.replies.insert(0, replies.pop(0))
                    await chats.wake(child.id)
            await asyncio.sleep(0.05)

    return asyncio.create_task(run())


async def test_a_foreground_agent_returns_its_report_and_its_id(chats):
    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", "call_agent", description="Read the tracker", prompt="Report the open items in the tracker.", run_in_background=False),
        _final_response("Done."),
    ]
    waker = await wake_children_as_they_come(chats, chat, [_final_response("Open: A, B and C.")])
    try:
        await chats.says(chat, "What is open?")
        await chats.wake(chat)
    finally:
        waker.cancel()
    [child] = await children(chats, chat)
    result = await tool_result(chats, chat, "call_agent")
    assert result.startswith("Open: A, B and C.")
    assert result.endswith(f"[agent_id: {child.id}]")
    assert child.config[DISPATCH_KEY]["mode"] == "foreground"
    # Its result is the call's: no notification, and its report is no news either.
    assert await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION]) == []


async def test_a_fork_starts_from_its_parents_conversation_up_to_the_call(chats):
    chat = await a_chat(chats, PARITY)
    await chats.says(chat, "The tracker is tracker.md; we agreed to skip archived items.")
    chats.replies = [_final_response("Noted.")]
    await chats.wake(chat)
    chats.replies = [
        calling("agent", "call_fork", description="Count open items", prompt="Count the open items.", fork=True, run_in_background=False),
        _final_response("Done."),
    ]
    waker = await wake_children_as_they_come(chats, chat, [_final_response("7 open items, archived ones skipped.")])
    try:
        await chats.says(chat, "Count them with a fork.")
        before = len(chats.requests)
        await chats.wake(chat)
    finally:
        waker.cancel()
    parents_request, childs_request = chats.requests[before], chats.requests[before + 1]
    # The fork read its parent's whole conversation up to the call, then its brief.
    assert childs_request[:-1] == parents_request
    assert childs_request[-1] == {"role": "user", "content": f"{agents_module.FORK_NOTE}\n\nCount the open items."}
    assert (await tool_result(chats, chat, "call_fork")).startswith("7 open items")


async def test_an_agent_at_the_depth_limit_cannot_start_one(chats):
    chat = await a_chat(chats, PARITY)
    parent = await chats.session(chat)
    from surogates.session.provisioning import create_child_session

    deepest = await create_child_session(
        store=chats.store, parent=parent, channel="delegation",
        config={"delegation_depth": delegate_module._MAX_DELEGATION_DEPTH, PARITY_KEY: True},
    )
    answer = json.loads(await agents_module._agent_handler(
        {"description": "Go deeper", "prompt": "Start another level."},
        session_store=chats.store, tenant=object(), session_id=str(deepest.id), budget=None,
    ))
    assert "depth limit" in answer["error"]
    assert await children(chats, deepest.id) == []
    # A child one level below the limit is a leaf: it is offered no agent tool.
    chats.replies = [
        calling("agent", "call_agent", description="Count open items", prompt="Count them.", run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "Count them.")
    await chats.wake(chat)
    [child] = [c for c in await children(chats, chat) if c.id != deepest.id]
    assert child.config["delegation_depth"] == 1
    chats.replies = [_final_response("Counted.")]
    await chats.wake(child.id)
    assert "agent" in chats.offered[-1]


async def test_a_client_cannot_name_the_familys_keys_or_its_depth(chats):  # noqa: F811
    created = await chats.api.client.post("/v1/sessions", json={"config": {
        PARITY_KEY: True, DISPATCH_KEY: {"mode": "background", "parent": "x"}, "agent_token_budget": 9,
        "agent_budget": {"tokens": 9}, "delegation_depth": -5, "keep": "me",
    }}, headers=chats.api.auth())
    config = (await chats.session(UUID(created.json()["id"]))).config
    assert config["keep"] == "me"
    assert not {PARITY_KEY, DISPATCH_KEY, "agent_token_budget", "agent_budget", "delegation_depth"} & set(config)


async def test_a_sub_agent_starts_no_agent_in_the_background(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    parent = await chats.session(chat)
    from surogates.session.provisioning import create_child_session

    child = await create_child_session(
        store=chats.store, parent=parent, channel="delegation", config={"delegation_depth": 1, PARITY_KEY: True},
    )
    answer = json.loads(await agents_module._agent_handler(
        {"description": "Go on", "prompt": "Do more.", "run_in_background": True},
        session_store=chats.store, tenant=object(), session_id=str(child.id), budget=None,
    ))
    assert "cannot start agents in the background" in answer["error"]


async def test_a_session_runs_at_most_five_agents_in_the_background(chats, monkeypatch):  # noqa: F811
    monkeypatch.setattr(agents_module, "MAX_RUNNING_IN_BACKGROUND", 2)
    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", f"call_{i}", description=f"Task {i}", prompt=f"Do task {i}.", run_in_background=True)
        for i in range(3)
    ] + [_final_response("Started.")]
    await chats.says(chat, "Start three.")
    await chats.wake(chat)
    started = [json.loads(await tool_result(chats, chat, f"call_{i}")) for i in range(3)]
    assert [s.get("status") for s in started[:2]] == ["running", "running"]
    assert "2 of your agents already run in the background" in started[2]["error"]
    assert len(await children(chats, chat)) == 2


async def test_a_background_agent_that_ends_early_says_why_and_closes_its_run_in_the_parents_log(chats):  # noqa: F811
    from surogates.harness.subagents import tell_parent

    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", "call_agent", description="Count open items", prompt="Count them.", run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "Count them.")
    await chats.wake(chat)
    [child] = await children(chats, chat)
    await tell_parent(store=chats.store, redis=None, session=child, status="completed", result="Partial.",
                      reason="tool_loop_halt")
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    content = notification_note(note.data)["content"]
    assert f'<summary>Agent {child.id} ("Count open items") ended early (tool_loop_halt); its result may be partial</summary>' in content
    assert "<result>\nPartial.\n</result>" in content
    [closed] = await chats.store.get_events(chat, types=[EventType.DELEGATION_COMPLETE])
    assert (closed.data["child_session_id"], closed.data["tool_call_id"]) == (str(child.id), "call_agent")


async def test_the_parent_is_told_even_when_its_log_cannot_be_closed(chats, monkeypatch):  # noqa: F811
    from surogates.harness.subagents import tell_parent
    from surogates.tools.builtin import delegate

    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", "call_agent", description="Count open items", prompt="Count them.", run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "Count them.")
    await chats.wake(chat)
    [child] = await children(chats, chat)

    async def broken(**_):
        raise RuntimeError("storage is away")

    monkeypatch.setattr(delegate, "_propagate_child_artifacts", broken)
    await tell_parent(store=chats.store, redis=None, session=child, status="completed", result="12.")
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["result"] == "12."


async def test_an_agent_runs_in_the_background_unless_asked_to_wait(chats, monkeypatch):  # noqa: F811
    # Before the change the call would wait for a report nothing writes: fail fast instead.
    monkeypatch.setattr(delegate_module, "_DELEGATION_TIMEOUT_SECONDS", 2)
    chat = await a_chat(chats, PARITY)
    chats.replies = [calling("agent", "call_a", description="Count", prompt="Count."), _final_response("Started.")]
    await chats.says(chat, "Count them.")
    await chats.wake(chat)
    assert json.loads(await tool_result(chats, chat, "call_a"))["status"] == "running"
    [child] = await children(chats, chat)
    assert child.config[DISPATCH_KEY]["mode"] == "background"


async def test_an_agent_started_without_the_flag_that_ends_mid_turn_is_read_before_the_next_request(chats, monkeypatch):  # noqa: F811
    monkeypatch.setattr(delegate_module, "_DELEGATION_TIMEOUT_SECONDS", 2)
    chat = await a_chat(chats, PARITY)
    asked = "Count them, then note it."
    chats.replies = [
        calling("agent", "call_a", description="Count open items", prompt="Count them."),
        calling("todo", "call_t", todos=[]),
        _final_response("Done."),
    ]

    async def the_agent_ends():
        # While the chat's own tool call runs, its agent's turn completes.
        [child] = await children(chats, chat)
        chats.replies.insert(0, _final_response("There are 12."))
        await chats.wake(child.id)

    chats.during_the_tool_call = the_agent_ends
    before = len(chats.requests)
    await chats.says(chat, asked)
    await chats.wake(chat)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    mine = [r for r in chats.requests[before:] if any(m.get("content") == asked for m in r)]
    assert notification_note(note.data) in mine[-1] and notification_note(note.data) not in mine[-2]
    sent = mine[-1]
    await chats.says(chat, "Thanks.")
    await chats.wake(chat)
    assert chats.requests[-1][: len(sent)] == sent


async def test_a_sub_agent_that_does_not_say_starts_its_agent_in_the_foreground(chats, monkeypatch):  # noqa: F811
    # Guards the default for a session with a parent: its call waits for the report.
    monkeypatch.setattr(delegate_module, "_DELEGATION_TIMEOUT_SECONDS", 2)
    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", "call_a", description="Count open items", prompt="Count them.", run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "Count them.")
    await chats.wake(chat)
    [child] = await children(chats, chat)
    chats.replies = [
        calling("agent", "call_inner", description="Read the log", prompt="Report the last line of the log."),
        _final_response("Counted."),
    ]
    waker = await wake_children_as_they_come(chats, child.id, [_final_response("The last line is OK.")])
    try:
        await chats.wake(child.id)
    finally:
        waker.cancel()
    [grandchild] = await children(chats, child.id)
    result = await tool_result(chats, child.id, "call_inner")
    assert grandchild.config[DISPATCH_KEY]["mode"] == "foreground"
    assert "error" not in result and result.startswith("The last line is OK.")


async def test_a_sixth_agent_without_the_flag_runs_in_the_foreground(chats, monkeypatch):  # noqa: F811
    monkeypatch.setattr(delegate_module, "_DELEGATION_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(agents_module, "MAX_RUNNING_IN_BACKGROUND", 1)
    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", "call_a", description="Count open items", prompt="Count them.", run_in_background=True),
        _final_response("Started."),
    ]
    await chats.says(chat, "Count them.")
    await chats.wake(chat)
    [first] = await children(chats, chat)
    # The first is never woken, so it keeps the limit full.
    chats.replies = [
        calling("agent", "call_b", description="Read the log", prompt="Report the last line of the log."),
        _final_response("Done."),
    ]
    waker = await wake_children_as_they_come(chats, chat, [_final_response("The last line is OK.")], skip={first.id})
    try:
        await chats.says(chat, "And the log?")
        await chats.wake(chat)
    finally:
        waker.cancel()
    [second] = [c for c in await children(chats, chat) if c.id != first.id]
    assert second.config[DISPATCH_KEY]["mode"] == "foreground"
    assert (await tool_result(chats, chat, "call_b")).startswith("The last line is OK.")
    assert [n for n in await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
            if n.data["agent_id"] == str(second.id)] == []
