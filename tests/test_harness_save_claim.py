"""A reply that says it saved something to memory with no memory call this turn."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from surogates.harness.budget import IterationBudget
from surogates.harness.loop_deliverables import SAVE_CLAIM_NUDGE, unbacked_save_claim
from surogates.session.models import Session
from surogates.tools.registry import ToolRegistry, ToolSchema

from tests.test_harness_resilience import _make_harness


def _memory_call(call_id: str = "m1") -> dict:
    return {
        "role": "assistant",
        "tool_calls": [{"id": call_id, "function": {"name": "memory", "arguments": "{}"}}],
    }


def _result(content: str, call_id: str = "m1") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


_ASK = {"role": "user", "content": "I prefer replies in Romanian, remember that."}


def test_a_save_claim_without_a_memory_call_is_flagged():
    assert unbacked_save_claim("Got it, noted it for next time.", [_ASK])
    assert unbacked_save_claim("I’ll remember that you prefer Romanian.", [_ASK])
    assert unbacked_save_claim("I've saved your preference to memory.", [_ASK])


def test_a_romanian_save_claim_is_flagged():
    assert unbacked_save_claim("Am notat, data viitoare îți răspund în română.", [_ASK])
    assert unbacked_save_claim("Perfect, o să țin minte asta.", [_ASK])
    assert unbacked_save_claim("Am salvat preferința în memorie.", [_ASK])


def test_a_successful_memory_call_this_turn_backs_the_claim():
    messages = [_ASK, _memory_call(), _result(json.dumps({"success": True}))]
    assert not unbacked_save_claim("Noted it.", messages)


def test_a_failed_memory_call_does_not_back_the_claim():
    messages = [_ASK, _memory_call(), _result(json.dumps({"success": False, "error": "x"}))]
    assert unbacked_save_claim("Noted it.", messages)


def test_a_memory_call_in_an_earlier_turn_does_not_back_this_one():
    messages = [
        {"role": "user", "content": "remember my timezone"},
        _memory_call(), _result(json.dumps({"success": True})),
        {"role": "assistant", "content": "Done."},
        _ASK,
    ]
    assert unbacked_save_claim("Noted it.", messages)


def test_a_memory_call_before_a_harness_nudge_still_counts():
    messages = [
        _ASK, _memory_call(), _result(json.dumps({"success": True})),
        {"role": "assistant", "content": "Let me also"},
        {"role": "user", "content": "[System: Your last message described ...]"},
    ]
    assert not unbacked_save_claim("Noted it.", messages)


def test_ordinary_replies_are_not_flagged():
    for text in (
        "Noted.",
        "I saved the report to outputs/report.docx.",
        "I noted three errors in the file.",
        "Am salvat fișierul.",
    ):
        assert not unbacked_save_claim(text, [_ASK]), text


def _session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web",
        status="active", config={}, created_at=now, updated_at=now,
    )


async def _run(monkeypatch, *, with_memory: bool) -> list[list[dict]]:
    registry = ToolRegistry()
    if with_memory:
        registry.register(
            name="memory",
            schema=ToolSchema(name="memory", description="m", parameters={"type": "object", "properties": {}}),
            handler=AsyncMock(return_value="{}"),
            toolset="memory",
        )
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_harness(
        session_store=store,
        tool_registry=registry,
        budget=IterationBudget(max_total=5),
        context_compressor=SimpleNamespace(context_length=1000),
        prompt_builder=SimpleNamespace(has_agents=False),
        sandbox_pool=None,
    )
    harness._streaming_enabled = False
    harness._prefetch_memory = AsyncMock(return_value="")
    harness._maybe_consult_required_expert = AsyncMock(return_value=None)
    harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
    harness._maybe_generate_title = MagicMock(return_value=None)
    harness._promote_fenced_artifacts = AsyncMock(return_value=None)
    harness._complete_session = AsyncMock(return_value=None)
    harness._end_turn = AsyncMock(return_value=None)

    sent: list[list[dict]] = []

    async def fake_call_llm_with_retry(**kwargs: Any) -> tuple[dict, dict]:
        sent.append(list(kwargs["create_kwargs"]["messages"]))
        return (
            {"role": "assistant", "content": "Noted it.", "tool_calls": None},
            {"model": "m", "finish_reason": "stop", "input_tokens": 1, "output_tokens": 1},
        )

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry)
    await harness._run_loop(
        _session(), [dict(_ASK)], "system", SimpleNamespace(lease_token=uuid4()), all_events=[],
    )
    harness._complete_session.assert_awaited_once()
    return sent


async def test_the_loop_nudges_once_then_accepts(monkeypatch):
    sent = await _run(monkeypatch, with_memory=True)
    assert len(sent) == 2
    assert sent[1][-1] == {"role": "user", "content": SAVE_CLAIM_NUDGE}


async def test_no_nudge_when_memory_is_not_offered(monkeypatch):
    sent = await _run(monkeypatch, with_memory=False)
    assert len(sent) == 1
