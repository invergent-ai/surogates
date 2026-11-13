"""A sub-agent's token budget: past it, its turn ends, and the agent that started it is told why."""

from __future__ import annotations

import json

import pytest

from surogates.harness.agent_news import notification_note
from surogates.harness.subagents import (
    AGENT_BUDGET_KEY,
    BUDGET_KEY,
    BUDGET_REASON,
    TOKEN_BUDGET_REPORT_REQUEST,
    budget_report,
)
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_agent_tool import a_chat, calling, chats, children, tool_result  # noqa: F401  (chats is a fixture)
from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
from .test_workstream_threads import TODO_CALL
from .test_send_message import a_background_agent, send

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Each scripted call uses 2 tokens (1 in, 1 out): a budget of 3 allows two calls.
BUDGETED = {BUDGET_KEY: 3}


async def test_a_background_agent_stops_on_its_budget_and_its_parent_is_told(chats):  # noqa: F811
    chat = await a_chat(chats, BUDGETED)
    child = await a_background_agent(chats, chat)
    assert child.config[AGENT_BUDGET_KEY] == {"tokens": 3}
    chats.replies = [TODO_CALL, TODO_CALL, _final_response("Report: 2 of 12 items counted.")]
    before = len(chats.requests)
    await chats.wake(child.id)
    # Two calls, then one last call asking for its report, with no tools.
    assert len(chats.requests) - before == 3
    assert chats.requests[-1][-1] == {"role": "user", "content": TOKEN_BUDGET_REPORT_REQUEST}
    [end] = await chats.store.get_events(child.id, types=[EventType.SESSION_COMPLETE])
    assert end.data["reason"] == "token_budget_exhausted"
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["status"] == "budget" and note.data["budget_tokens"] == 3
    # Its report, never a line from the middle of a step.
    assert note.data["result"] == "Report: 2 of 12 items counted."
    assert (
        f'<summary>Agent {child.id} ("Count open items") used its budget of 3 tokens before it finished; '
        "its result is partial</summary>"
    ) in notification_note(note.data)["content"]
    chats.replies = []


async def test_a_foreground_agent_that_runs_out_says_so_in_its_result(chats):  # noqa: F811
    from .test_agent_tool import wake_children_as_they_come

    chat = await a_chat(chats, {BUDGET_KEY: 1})
    chats.replies = [
        calling("agent", "call_agent", description="Read the tracker", prompt="Report the open items in the tracker.",
                run_in_background=False),
        _final_response("Report: none counted yet."),
        _final_response("Done."),
    ]
    waker = await wake_children_as_they_come(chats, chat, [TODO_CALL])
    try:
        await chats.says(chat, "What is open?")
        await chats.wake(chat)
    finally:
        waker.cancel()
    result = await tool_result(chats, chat, "call_agent")
    assert result.startswith("Report: none counted yet.\n\n[It stopped before it finished: it used its token budget.]")


async def test_without_a_budget_an_agent_runs_as_before(chats):  # noqa: F811
    chat = await a_chat(chats, {})
    child = await a_background_agent(chats, chat)
    assert AGENT_BUDGET_KEY not in child.config
    chats.replies = [TODO_CALL, TODO_CALL, TODO_CALL, _final_response("12 open items.")]
    await chats.wake(child.id)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["status"] == "completed" and json.dumps(note.data).count("budget") == 0


async def test_an_agent_that_used_its_budget_is_not_resumed_by_a_message(chats):  # noqa: F811
    chat = await a_chat(chats, BUDGETED)
    child = await a_background_agent(chats, chat)
    chats.replies = [TODO_CALL, TODO_CALL, _final_response("Report.")]
    await chats.wake(child.id)
    assert await chats.status(child.id) == "completed"
    answer = await send(chats, chat, child.id)
    assert answer["status"] == "queued" and "it used its token budget" in answer["note"]
    assert await chats.status(child.id) == "completed"
    assert len(await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])) == 1


async def test_an_agents_own_agents_get_at_most_what_it_has_left(chats):  # noqa: F811
    from .test_agent_tool import wake_children_as_they_come

    chat = await a_chat(chats, {BUDGET_KEY: 10})
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
    # The sub-agent had used some of its 10 tokens when it started its own: that one gets half the rest.
    assert 0 < inner.config[AGENT_BUDGET_KEY]["tokens"] < 5


async def test_agents_started_at_once_share_what_their_parent_has_left(chats, monkeypatch):  # noqa: F811
    import asyncio

    from surogates.tools.builtin import agents as agents_module, delegate

    handed: list[int] = []

    async def run(**kwargs):
        handed.append((kwargs["extra_config"].get(AGENT_BUDGET_KEY) or {}).get("tokens"))
        await asyncio.sleep(0.2)
        return "Done."

    chat = await a_chat(chats, BUDGETED)
    child = await a_background_agent(chats, chat)
    await chats.store.update_session_config_key(child.id, AGENT_BUDGET_KEY, {"tokens": 100})
    # From here, the agents' starts are recorded, not run.
    monkeypatch.setattr(delegate, "_run_single_delegation", run)

    def call(n):
        return agents_module._agent_handler(
            {"description": f"Part {n}", "prompt": f"Read part {n} of the log."},
            session_store=chats.store, session_id=str(child.id), tenant=object(), tool_call_id=f"call_{n}",
        )

    results = await asyncio.gather(call(1), call(2), call(3))
    assert all(r.startswith("Done.") for r in results)
    # Half of what was left for each, in turn: never more than the parent had.
    assert sorted(handed, reverse=True) == [50, 25, 12] and sum(handed) <= 100
    assert agents_module._BUDGET_HANDED == {}


async def test_the_iteration_cap_of_an_agent_without_a_token_budget_is_no_budget_stop(chats):  # noqa: F811
    chat = await a_chat(chats, {})
    child = await a_background_agent(chats, chat)
    # The test worker's iteration budget is 10: the eleventh step asks for a summary and ends there.  Each step reads
    # a part of its own, so no loop guard ends the turn first.
    chats.replies = [calling("read_file", f"call_{n}", path=f"part{n}.md") for n in range(10)] + [
        _final_response("Two items so far."),
    ]
    await chats.wake(child.id)
    [end] = await chats.store.get_events(child.id, types=[EventType.SESSION_COMPLETE])
    assert end.data["reason"] == "budget_exhausted"
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["status"] == "completed" and "budget_tokens" not in note.data
    chats.replies = []


async def test_a_resumed_agent_that_runs_out_reports_this_runs_words_not_its_first_report(chats):  # noqa: F811
    chat = await a_chat(chats, {BUDGET_KEY: 5})
    child = await a_background_agent(chats, chat)
    chats.replies = [_final_response("First report: 12 open items.")]
    await chats.wake(child.id)
    assert (await send(chats, chat, child.id))["status"] == "resumed"
    chats.replies = [TODO_CALL, TODO_CALL, _final_response("Second report: 3 closed.")]
    await chats.wake(child.id)
    first, second = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert first.data["result"] == "First report: 12 open items."
    assert second.data["status"] == "budget" and second.data["result"] == "Second report: 3 closed."
    chats.replies = []


async def test_a_budget_report_is_the_last_tools_off_reply_or_nothing():
    from types import SimpleNamespace

    def event(kind, **data):
        return SimpleNamespace(type=kind.value, data=data)

    step = event(EventType.LLM_RESPONSE, message={"content": "Let me check the next file."})
    report = event(EventType.LLM_RESPONSE, message={"content": "Done so far: 4."}, finish_reason=BUDGET_REASON)
    # The call for the report failed: nothing, never the mid-step line.
    assert budget_report([step, event(EventType.SESSION_COMPLETE)]) == ""
    assert budget_report([step, report, event(EventType.SESSION_COMPLETE)]) == "Done so far: 4."
    # A report of an earlier run is no report of this one.
    assert budget_report([report, event(EventType.SESSION_COMPLETE), step, event(EventType.SESSION_COMPLETE)]) == ""
    # Events after the run's own end (a caption, a message) do not hide its report.
    later = event(EventType.COORDINATOR_MESSAGE, content="Also count the closed ones.")
    assert budget_report([step, report, event(EventType.SESSION_COMPLETE), later]) == "Done so far: 4."
