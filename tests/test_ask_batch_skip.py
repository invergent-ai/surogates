"""A question that came back unanswered skips the rest of its batch, also with loop guidance on it."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from surogates.harness import loop, streaming_executor, tool_exec
from surogates.harness.message_utils import make_skipped_tool_result
from surogates.harness.tool_exec import SKIPPED_BEHIND_QUESTION
from surogates.harness.tool_guardrails import append_toolguard_guidance
from surogates.session.events import EventType

ASK = {"id": "a", "type": "function", "function": {"name": "ask_user_question", "arguments": "{}"}}
AFTER = {"id": "b", "type": "function", "function": {"name": "todo", "arguments": "{}"}}
TERMINAL = {"id": "t", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}


class Store:
    def __init__(self):
        self.events: list[tuple] = []

    async def emit_event(self, _session_id, event_type, data, **__):
        self.events.append((event_type, data.get("tool_call_id")))
        return 1

    async def advance_harness_cursor(self, *_, **__):
        pass


class Warns:
    """Guardrails that append a warning to every result, as a repeated call gets."""
    halt_decision = None

    def before_call(self, *_):
        return SimpleNamespace(allows_execution=True)

    def after_call(self, *_):
        return SimpleNamespace(action="warn", message="again", code="c", count=2)


@pytest.fixture
def ran(monkeypatch):
    ran: list[str] = []

    async def single(tc, **_):
        name = tc["function"]["name"]
        ran.append(name)
        content = json.dumps({"cancelled": True, "reason": "timeout"}) if name == "ask_user_question" else "{}"
        return {"role": "tool", "tool_call_id": tc["id"], "content": content}

    monkeypatch.setattr(tool_exec, "execute_single_tool", single)
    return ran


def kw(store=None):
    return dict(session=SimpleNamespace(id=1, config={}), lease=SimpleNamespace(lease_token="t"), store=store or Store(),
                tools=None, tenant=None, interrupt_check=lambda: False, tool_guardrails=Warns())


async def test_sequential_skips_after_an_unanswered_question_with_guidance(ran):
    results = await tool_exec.execute_tool_calls_sequential([ASK, AFTER], **kw())
    assert ran == ["ask_user_question"]
    assert SKIPPED_BEHIND_QUESTION in results[1]["content"]


async def test_streaming_skips_after_an_unanswered_question_with_guidance(ran):
    executor = streaming_executor.StreamingToolExecutor(**kw())
    for tc in (ASK, AFTER):
        executor.add_tool(tc)
    results = await executor.get_all_results()
    assert ran == ["ask_user_question"]
    assert SKIPPED_BEHIND_QUESTION in results[1]["content"]


async def test_streaming_skips_each_call_after_an_unanswered_question_with_its_events(ran):
    """A skipped ``terminal`` is no failed one: the call after it is skipped as itself, not aborted unseen."""
    store = Store()
    executor = streaming_executor.StreamingToolExecutor(**kw(store))
    for tc in (ASK, TERMINAL, AFTER):
        executor.add_tool(tc)
    results = await executor.get_all_results()
    assert ran == ["ask_user_question"]
    for tc, result in zip((TERMINAL, AFTER), results[1:]):
        assert result["content"] == make_skipped_tool_result(tc, SKIPPED_BEHIND_QUESTION)["content"]
        assert (EventType.TOOL_CALL, tc["id"]) in store.events
        assert (EventType.TOOL_RESULT, tc["id"]) in store.events


def test_a_timed_out_question_with_guidance_on_its_result_still_ends_the_turn():
    warned = append_toolguard_guidance(
        json.dumps({"cancelled": True, "reason": "timeout"}),
        SimpleNamespace(action="warn", message="again", code="c", count=2),
    )
    assert loop._unanswered_question([ASK], [{"tool_call_id": "a", "content": warned}]) == "timeout"
