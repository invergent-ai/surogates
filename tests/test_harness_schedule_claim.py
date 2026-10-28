"""A reply that says a schedule exists with no schedule tool backing it this turn."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from surogates.harness.budget import IterationBudget
from surogates.harness.loop_deliverables import schedule_claim_nudge, unbacked_schedule_claim
from surogates.harness.prompt import scheduled_run_block
from surogates.session.models import Session
from surogates.tools.registry import ToolRegistry

from tests.test_harness_resilience import _make_harness

_ASK = {"role": "user", "content": "Post the digest every weekday at 9."}


def _call(name: str, call_id: str = "c1") -> dict:
    return {
        "role": "assistant",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": "{}"}}],
    }


def _result(content: dict, call_id: str = "c1") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(content)}


def test_claims_that_a_schedule_exists_are_flagged_in_english_and_romanian():
    for text in (
        "I've scheduled it for every weekday at 9.",
        "The loop is now active.",
        "Your routine has been set up.",
        "It will run every day at 09:00.",
        "Am programat postarea zilnică.",
        "Bucla este activă.",
        "Este programată pentru ora 9.",
    ):
        assert unbacked_schedule_claim(text, [_ASK]), text


def test_a_successful_schedule_call_this_turn_backs_the_claim():
    for name in ("cron_create", "cron_list", "cron_resume"):
        messages = [_ASK, _call(name), _result({"success": True})]
        assert not unbacked_schedule_claim("I've scheduled it.", messages), name


def test_a_preview_or_an_unrelated_call_does_not_back_it():
    preview = [_ASK, _call("cron_create"), _result({"success": False, "error": "x", "would": {}})]
    assert unbacked_schedule_claim("The schedule is active.", preview)
    other = [_ASK, _call("cron_delete"), _result({"success": True})]
    assert unbacked_schedule_claim("The schedule is active.", other)


def test_a_refused_call_already_told_the_model_why():
    refused = [_ASK, _call("cron_create"), _result({"success": False, "error": "Not in this chat."})]
    assert not unbacked_schedule_claim("The schedule is active.", refused)


def test_ordinary_replies_are_not_flagged():
    for text in (
        "Should I schedule this for 9:00 Europe/Bucharest?",
        "It is not scheduled yet.",
        "The meeting is scheduled for Monday in your calendar.",
    ):
        assert not unbacked_schedule_claim(text, [_ASK]), text


def test_the_nudge_says_whether_a_schedule_can_be_made_here():
    assert "cron_create" in schedule_claim_nudge(True)
    assert "not scheduled" in schedule_claim_nudge(False)


def test_a_scheduled_run_is_told_which_schedule_and_run_it_is():
    block = scheduled_run_block({
        "id": "s1", "name": "digest", "cadence": "Weekdays at 09:00", "timezone": "Europe/Bucharest",
        "run_number": 3, "repeat_limit": 10,
        "previous_run": {"status": "completed", "started_at": "2026-10-27T09:00:00+02:00"},
        "next_run_at": "2026-10-29T09:00:00+02:00", "last_error": "timeout",
    })
    assert "digest (id s1)" in block
    assert "Run number: 3 of 10" in block
    assert "Previous run: completed" in block
    assert "Last problem: timeout" in block
    assert "do not make another" in block
    first = scheduled_run_block({"id": "s1", "name": "digest", "run_number": 1})
    assert "none, this is the first" in first
    assert "none planned" in first


def test_a_scheduled_run_is_pointed_only_at_the_tools_it_has():
    run = {"id": "s1", "name": "digest", "run_number": 1}
    assert "cron_pause, cron_delete with id s1" in scheduled_run_block(run, offered={"cron_pause", "cron_delete"})
    assert "cron_" not in scheduled_run_block(run, offered={"web_search"})


def _session(config: dict) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web",
        status="active", config=config, created_at=now, updated_at=now,
    )


async def _run(monkeypatch, config: dict) -> list[list[dict]]:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_harness(
        session_store=store,
        tool_registry=ToolRegistry(),
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
            {"role": "assistant", "content": "I've scheduled it for every weekday at 9.", "tool_calls": None},
            {"model": "m", "finish_reason": "stop", "input_tokens": 1, "output_tokens": 1},
        )

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry)
    await harness._run_loop(
        _session(config), [dict(_ASK)], "system", SimpleNamespace(lease_token=uuid4()), all_events=[],
    )
    harness._complete_session.assert_awaited_once()
    return sent


async def test_the_loop_nudges_once_then_accepts(monkeypatch):
    sent = await _run(monkeypatch, {})
    assert len(sent) == 2
    assert sent[1][-1] == {"role": "user", "content": schedule_claim_nudge(False)}


async def test_a_scheduled_run_is_not_nudged(monkeypatch):
    sent = await _run(monkeypatch, {"scheduled_session_id": "s1", "scheduled_run": {"id": "s1"}})
    assert len(sent) == 1
