"""A ``delegate_task`` helper's result reaches its parent once: as the call's result.

The helper's turn end also writes a report to its parent, as any helper's
does.  The parent waits for the helper inside the call and already has the
result, so the live turn never read that report.  A later turn rebuilds the
conversation from the log, and replay read it as news before the next
request: the helper's result a second time, in a request the live turn
never sent.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.harness.loop_context_replay import unread_reports
from surogates.harness.loop_pending import GAVE_UP_ON_HELPER, _actionable_pending_events
from surogates.harness.worker_notify import is_delegated, notify_parent_on_failure
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from tests.test_steer_loop import _final_response

from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
from .test_workstream_threads import answered, turn_ends

pytestmark = pytest.mark.asyncio(loop_scope="session")


def delegating(goal: str) -> tuple[dict, dict]:
    return (
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_delegate", "type": "function", "function": {"name": "delegate_task", "arguments": f'{{"goal": "{goal}"}}'}},
        ]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )


@pytest.mark.parametrize("ends", ["completed", "failed"])
async def test_a_delegated_helpers_report_is_not_read_again_after_its_calls_result(workers, ends):  # noqa: F811
    chat = await workers.chat(talk=())
    helpers: list[UUID] = []

    async def the_helper_runs_and_ends():
        # The call's helper does its turn while the parent waits in the call.
        helper = await create_child_session(store=workers.store, parent=await workers.session(chat), channel="delegation")
        helpers.append(helper.id)
        if ends == "completed":
            await answered(workers.api, helper, "There are 12 open items.")
            await turn_ends(workers.api, helper)
            # What delegate_task writes once it has the helper's result.
            await workers.store.emit_event(chat, EventType.DELEGATION_COMPLETE, {"child_session_id": str(helper.id)})
        else:
            await notify_parent_on_failure(
                session_store=workers.store, worker_session_id=helper.id, parent_session_id=chat,
                org_id=str(helper.org_id), agent_id=helper.agent_id, error="boom",
                redis=workers.api.app.state.redis, delegated=is_delegated(helper),
            )
            await workers.store.emit_event(
                chat, EventType.DELEGATION_FAILED, {"child_session_id": str(helper.id), "reason": "Child session failed: boom"},
            )

    workers.during_the_tool_call = the_helper_runs_and_ends
    workers.replies = [delegating("Count the open items"), _final_response("There are 12.")]
    await workers.says(chat, "How many open items are there?")
    await workers.wake(chat)

    [report] = await workers.store.get_events(chat, types=[EventType.WORKER_COMPLETE, EventType.WORKER_FAILED])
    assert report.data["worker_id"] == str(helpers[0]) and report.data["delegated"] is True
    after_the_call = workers.requests[-1]
    assert after_the_call[-1]["role"] == "tool"
    # It is no news for a later wake either.
    assert unread_reports(await workers.store.get_events(chat)) == []

    await workers.says(chat, "And closed ones?")
    await workers.wake(chat)
    # The next turn's request starts with the conversation the live turn had: nothing read twice.
    assert workers.requests[-1][: len(after_the_call)] == after_the_call
    assert not any("[Worker" in str(m.get("content")) for m in workers.requests[-1])


async def test_a_spawned_workers_report_is_still_read(workers):  # noqa: F811
    chat = await workers.chat(talk=())
    helper = await create_child_session(store=workers.store, parent=await workers.session(chat), channel="worker")
    await answered(workers.api, helper, "Checked the figures.")
    await turn_ends(workers.api, helper)
    [report] = await workers.store.get_events(chat, types=[EventType.WORKER_COMPLETE])
    assert "delegated" not in report.data
    assert unread_reports(await workers.store.get_events(chat)) == [
        {"role": "user", "content": f"[Worker {helper.id} completed]\nChecked the figures."},
    ]


@pytest.mark.parametrize("call", ["timed out", "answered"])
async def test_a_helper_report_after_its_call_returned_is_news_only_if_the_call_gave_up(workers, call):  # noqa: F811
    chat = await workers.chat(talk=())
    helper = await create_child_session(store=workers.store, parent=await workers.session(chat), channel="delegation")
    store = workers.store
    await store.emit_event(chat, EventType.DELEGATION_START, {"child_session_id": str(helper.id), "goal": "Count"})
    if call == "timed out":
        await store.emit_event(chat, EventType.DELEGATION_FAILED, {"child_session_id": str(helper.id), "reason": GAVE_UP_ON_HELPER})
    else:
        await store.emit_event(chat, EventType.DELEGATION_COMPLETE, {"child_session_id": str(helper.id), "goal": "Count"})
    await store.emit_event(chat, EventType.LLM_REQUEST, {})
    cursor = (await store.get_events(chat))[-1].id
    # The helper's report lands after its call returned, and after the parent's next request.
    await answered(workers.api, helper, "There are 12 open items.")
    await turn_ends(workers.api, helper)
    events = await store.get_events(chat)
    pending = [e.type for e in _actionable_pending_events(events, cursor)]
    if call == "timed out":
        # The parent never got it: it is the parent's news, and work for a wake.
        assert unread_reports(events) == [{"role": "user", "content": f"[Worker {helper.id} completed]\nThere are 12 open items."}]
        assert EventType.WORKER_COMPLETE.value in pending
    else:
        assert unread_reports(events) == []
        assert EventType.WORKER_COMPLETE.value not in pending
