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
from surogates.harness.agent_news import notification_note
from surogates.harness.subagents import DISPATCH_KEY, PARITY_KEY
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
REAL = frozenset({"agent"})


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
        return harness

    workers.worker = worker
    model = loop_module.call_llm_with_retry

    async def offered(**kwargs):
        workers.offered.append([t["function"]["name"] for t in kwargs["create_kwargs"].get("tools") or []])
        return await model(**kwargs)

    monkeypatch.setattr(loop_module, "call_llm_with_retry", offered)
    return workers


async def a_chat(chats, config: dict | None = None) -> UUID:
    created = await chats.api.client.post("/v1/sessions", json={"config": config or {}}, headers=chats.api.auth())
    assert created.status_code == 201, created.text
    return UUID(created.json()["id"])


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
    # Everything else is offered alike, in the same order.
    assert [t for t in off_tools if t != "delegate_task"] == [t for t in on_tools if t != "agent"]


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
    assert note.data == {
        "agent_id": str(child.id), "kind": "agent", "status": "completed", "summary": "Count open items",
        "result": "There are 12 open items.", "tool_call_id": "call_agent",
    }
    assert await queued(chats.api, await chats.session(chat))

    before = len(chats.requests)
    chats.replies = [_final_response("There are 12.")]
    await chats.wake(chat)
    request = chats.requests[before]
    assert request[-1] == notification_note(note.data)
    assert request[-1]["content"] == f"[Agent {child.id} (Count open items) completed]\nThere are 12 open items."
    await chats.says(chat, "Thanks.")
    await chats.wake(chat)
    assert chats.requests[-1][: len(request)] == request


async def wake_children_as_they_come(chats, chat: UUID, replies: list) -> asyncio.Task:
    """Wake each sub-agent of *chat* once it is created, answering its first request with the next of *replies*."""
    async def run():
        woken: set[UUID] = set()
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
        calling("agent", "call_agent", description="Read the tracker", prompt="Report the open items in the tracker."),
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
        calling("agent", "call_fork", description="Count open items", prompt="Count the open items.", fork=True),
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
