"""A sub-agent's token budget: past it, its turn ends, and the agent that started it is told why."""

from __future__ import annotations

import json

import pytest

from surogates.harness.agent_news import notification_note
from surogates.harness.subagents import AGENT_BUDGET_KEY, BUDGET_KEY, PARITY_KEY
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_agent_tool import a_chat, calling, chats, children, tool_result  # noqa: F401  (chats is a fixture)
from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
from .test_workstream_threads import TODO_CALL
from .test_send_message import a_background_agent, send

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Each scripted call uses 2 tokens (1 in, 1 out): a budget of 3 allows two calls.
BUDGETED = {PARITY_KEY: True, BUDGET_KEY: 3}


async def test_a_background_agent_stops_on_its_budget_and_its_parent_is_told(chats):  # noqa: F811
    chat = await a_chat(chats, BUDGETED)
    child = await a_background_agent(chats, chat)
    assert child.config[AGENT_BUDGET_KEY] == {"tokens": 3}
    chats.replies = [TODO_CALL, TODO_CALL, _final_response("never asked")]
    before = len(chats.requests)
    await chats.wake(child.id)
    # Two calls, then the turn ended before a third.
    assert len(chats.requests) - before == 2
    [end] = await chats.store.get_events(child.id, types=[EventType.SESSION_COMPLETE])
    assert end.data["reason"] == "token_budget_exhausted"
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["status"] == "budget" and note.data["budget_tokens"] == 3
    assert notification_note(note.data)["content"].startswith(
        f"[Agent {child.id} (Count open items) stopped before it finished: it used its budget of 3 tokens]",
    )
    chats.replies = []


async def test_a_foreground_agent_that_runs_out_says_so_in_its_result(chats):  # noqa: F811
    from .test_agent_tool import wake_children_as_they_come

    chat = await a_chat(chats, {PARITY_KEY: True, BUDGET_KEY: 1})
    chats.replies = [
        calling("agent", "call_agent", description="Read the tracker", prompt="Report the open items in the tracker."),
        _final_response("Done."),
    ]
    waker = await wake_children_as_they_come(chats, chat, [TODO_CALL])
    try:
        await chats.says(chat, "What is open?")
        await chats.wake(chat)
    finally:
        waker.cancel()
    result = await tool_result(chats, chat, "call_agent")
    assert "[It stopped before it finished: it used its token budget.]" in result


async def test_without_a_budget_an_agent_runs_as_before(chats):  # noqa: F811
    chat = await a_chat(chats, {PARITY_KEY: True})
    child = await a_background_agent(chats, chat)
    assert AGENT_BUDGET_KEY not in child.config
    chats.replies = [TODO_CALL, TODO_CALL, TODO_CALL, _final_response("12 open items.")]
    await chats.wake(child.id)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["status"] == "completed" and json.dumps(note.data).count("budget") == 0


async def test_an_agent_that_used_its_budget_is_not_resumed_by_a_message(chats):  # noqa: F811
    chat = await a_chat(chats, BUDGETED)
    child = await a_background_agent(chats, chat)
    chats.replies = [TODO_CALL, TODO_CALL]
    await chats.wake(child.id)
    assert await chats.status(child.id) == "completed"
    answer = await send(chats, chat, child.id)
    assert answer["status"] == "queued" and "it used its token budget" in answer["note"]
    assert await chats.status(child.id) == "completed"
    assert len(await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])) == 1


async def test_an_agents_own_agents_get_at_most_what_it_has_left(chats):  # noqa: F811
    from .test_agent_tool import wake_children_as_they_come

    chat = await a_chat(chats, {PARITY_KEY: True, BUDGET_KEY: 10})
    child = await a_background_agent(chats, chat)
    chats.replies = [
        TODO_CALL,
        calling("agent", "call_inner", description="Read the log", prompt="Report the last line of the build log."),
        _final_response("Done."),
    ]
    waker = await wake_children_as_they_come(chats, child.id, [_final_response("The build passed.")])
    try:
        await chats.wake(child.id)
    finally:
        waker.cancel()
    [inner] = await children(chats, child.id)
    # The sub-agent had used some of its 10 tokens when it started its own: that one gets the rest, no more.
    assert 0 < inner.config[AGENT_BUDGET_KEY]["tokens"] < 10


async def test_the_iteration_cap_of_an_agent_without_a_token_budget_is_no_budget_stop(chats, monkeypatch):  # noqa: F811
    from surogates.tools.builtin import delegate

    monkeypatch.setattr(delegate, "_CHILD_MAX_ITERATIONS", 1)
    chat = await a_chat(chats, {PARITY_KEY: True})
    child = await a_background_agent(chats, chat)
    chats.replies = [TODO_CALL, _final_response("Two items so far.")]
    await chats.wake(child.id)
    [end] = await chats.store.get_events(child.id, types=[EventType.SESSION_COMPLETE])
    assert end.data["reason"] == "budget_exhausted"
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["status"] == "completed" and "budget_tokens" not in note.data
    chats.replies = []
