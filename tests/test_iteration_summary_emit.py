"""iteration.summary emission from the harness loop."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from surogates.harness.budget import IterationBudget
from surogates.session.events import EventType
from tests.test_loop_turn_id import (
    _drive_run_loop,
    _make_loop_harness,
)


class _StubTurnSummarizer:
    """Scriptable stand-in for :class:`TurnSummarizer` used by A7/A8 tests."""

    def __init__(self) -> None:
        self.iteration_responses: list[str | None] = []
        self.turn_response: Any = "MISSING"
        self.iteration_calls: list[dict[str, Any]] = []
        self.turn_calls: list[dict[str, Any]] = []
        self._iter_idx = 0

    async def summarize_iteration(self, **kwargs: Any) -> str | None:
        self.iteration_calls.append(kwargs)
        if self._iter_idx >= len(self.iteration_responses):
            return None
        out = self.iteration_responses[self._iter_idx]
        self._iter_idx += 1
        return out

    async def summarize_turn(self, **kwargs: Any) -> Any:
        self.turn_calls.append(kwargs)
        if self.turn_response == "MISSING":
            return None
        return self.turn_response


@pytest.fixture
def stub_turn_summarizer() -> _StubTurnSummarizer:
    return _StubTurnSummarizer()


async def _drain_pending(harness) -> None:
    """Await any in-flight iteration/turn summary tasks for deterministic
    assertions on event ordering."""
    pending = list(harness._pending_iteration_summary_tasks.values())
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_iteration_summary_emitted_for_text_only_response(
    monkeypatch: pytest.MonkeyPatch,
    stub_turn_summarizer: _StubTurnSummarizer,
) -> None:
    """A single text-only iteration emits exactly one ITERATION_SUMMARY."""
    stub_turn_summarizer.iteration_responses = ["Replied without tools"]
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(
        session_store=store,
        turn_summarizer=stub_turn_summarizer,
    )

    emits = await _drive_run_loop(
        harness=harness,
        responses=[
            (
                {
                    "role": "assistant",
                    "content": "Done.",
                    "reasoning": "Thinking briefly.",
                    "tool_calls": None,
                },
                {"model": "test", "finish_reason": "stop",
                 "input_tokens": 1, "output_tokens": 1},
            ),
        ],
        monkeypatch=monkeypatch,
    )
    await _drain_pending(harness)

    summaries = [p for _, t, p in emits if t == EventType.ITERATION_SUMMARY]
    # Background task may have landed after _drive_run_loop captured the
    # initial emit list — re-pull from the store mock.
    summaries = [
        c.args[2]
        for c in store.emit_event.await_args_list
        if c.args[1] == EventType.ITERATION_SUMMARY
    ]
    assert len(summaries) == 1
    payload = summaries[0]
    assert payload["summary"] == "Replied without tools"
    assert payload["iteration_index"] == 0
    assert payload["tool_call_ids"] == []
    assert payload["turn_id"]
    assert payload["started_at"]
    assert payload["ended_at"]


@pytest.mark.asyncio
async def test_iteration_summary_skipped_when_summarizer_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(200, 300))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(session_store=store, turn_summarizer=None)

    await _drive_run_loop(
        harness=harness,
        responses=[
            (
                {"role": "assistant", "content": "Done.", "tool_calls": None},
                {"model": "test", "finish_reason": "stop",
                 "input_tokens": 1, "output_tokens": 1},
            ),
        ],
        monkeypatch=monkeypatch,
    )
    await _drain_pending(harness)

    summaries = [
        c.args[2]
        for c in store.emit_event.await_args_list
        if c.args[1] == EventType.ITERATION_SUMMARY
    ]
    assert summaries == []


@pytest.mark.asyncio
async def test_iteration_summary_skipped_when_summarizer_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    stub_turn_summarizer: _StubTurnSummarizer,
) -> None:
    stub_turn_summarizer.iteration_responses = [None]
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(300, 400))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(
        session_store=store, turn_summarizer=stub_turn_summarizer,
    )

    await _drive_run_loop(
        harness=harness,
        responses=[
            (
                {
                    "role": "assistant",
                    "content": "Done.",
                    "reasoning": "Briefly.",
                    "tool_calls": None,
                },
                {"model": "test", "finish_reason": "stop",
                 "input_tokens": 1, "output_tokens": 1},
            ),
        ],
        monkeypatch=monkeypatch,
    )
    await _drain_pending(harness)

    summaries = [
        c.args[2]
        for c in store.emit_event.await_args_list
        if c.args[1] == EventType.ITERATION_SUMMARY
    ]
    assert summaries == []
    # The summarizer WAS called (it produced None — it wasn't a no-op).
    assert len(stub_turn_summarizer.iteration_calls) == 1


@pytest.mark.asyncio
async def test_iteration_summary_skipped_when_iteration_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    stub_turn_summarizer: _StubTurnSummarizer,
) -> None:
    """If an iteration has no reasoning and no tool calls, never call the
    summarizer — that's a strong signal something else short-circuited."""
    stub_turn_summarizer.iteration_responses = ["should not be used"]
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(400, 500))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(
        session_store=store, turn_summarizer=stub_turn_summarizer,
    )

    await _drive_run_loop(
        harness=harness,
        responses=[
            (
                {"role": "assistant", "content": "Done.", "tool_calls": None},
                {"model": "test", "finish_reason": "stop",
                 "input_tokens": 1, "output_tokens": 1},
            ),
        ],
        monkeypatch=monkeypatch,
    )
    await _drain_pending(harness)

    summaries = [
        c.args[2]
        for c in store.emit_event.await_args_list
        if c.args[1] == EventType.ITERATION_SUMMARY
    ]
    # Reasoning is empty AND tool_calls is empty → no model call.
    assert summaries == []
    assert stub_turn_summarizer.iteration_calls == []


@pytest.mark.asyncio
async def test_iteration_summary_carries_turn_id_from_llm_response(
    monkeypatch: pytest.MonkeyPatch,
    stub_turn_summarizer: _StubTurnSummarizer,
) -> None:
    stub_turn_summarizer.iteration_responses = ["Did the thing"]
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(500, 600))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(
        session_store=store,
        budget=IterationBudget(max_total=2),
        turn_summarizer=stub_turn_summarizer,
    )

    await _drive_run_loop(
        harness=harness,
        responses=[
            (
                {
                    "role": "assistant",
                    "content": "Done.",
                    "reasoning": "x",
                    "tool_calls": None,
                },
                {"model": "test", "finish_reason": "stop",
                 "input_tokens": 1, "output_tokens": 1},
            ),
        ],
        monkeypatch=monkeypatch,
    )
    await _drain_pending(harness)

    summaries = [
        c.args[2]
        for c in store.emit_event.await_args_list
        if c.args[1] == EventType.ITERATION_SUMMARY
    ]
    responses = [
        c.args[2]
        for c in store.emit_event.await_args_list
        if c.args[1] == EventType.LLM_RESPONSE
    ]
    assert summaries and responses
    assert summaries[0]["turn_id"] == responses[0]["turn_id"]
