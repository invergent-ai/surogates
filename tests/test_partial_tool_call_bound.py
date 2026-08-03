"""The partial-tool-call recovery path must terminate.

``_run_loop`` refunds the iteration budget when a provider ends a response
before the tool-call arguments are complete, so the wake loop's
``while self._budget.remaining > 0`` guard never advances on that path.  A
provider that keeps truncating therefore spins forever: there is no
wall-clock deadline anywhere in the wake loop.

These tests pin the bound. They drive the real ``_run_loop`` body with a
provider that always truncates, and fail loudly instead of hanging.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.session.events import EventType

from tests.test_loop_turn_id import _make_loop_harness, _make_session

# Far above any legitimate retry ceiling, low enough that a runaway loop
# trips it in well under a second.
_RUNAWAY_THRESHOLD = 40


def _partial_tool_call_response() -> tuple[dict[str, Any], dict[str, Any]]:
    """A response whose tool-call arguments were cut off mid-JSON."""
    return (
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": '{"path": "a'},
                }
            ],
        },
        {
            "model": "test-model",
            "finish_reason": "tool_calls",
            "partial_tool_call": True,
            "partial_tool_names": ["write_file"],
            "input_tokens": 1,
            "output_tokens": 1,
        },
    )


@pytest.mark.asyncio
async def test_repeated_partial_tool_calls_terminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that always truncates must not spin the wake loop forever."""
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 100 + 4 * _RUNAWAY_THRESHOLD))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(
        session_store=store, budget=IterationBudget(max_total=5),
    )

    calls = 0

    async def always_partial(**_kwargs: Any) -> tuple[dict, dict]:
        nonlocal calls
        calls += 1
        if calls > _RUNAWAY_THRESHOLD:
            raise AssertionError(
                f"_run_loop made {calls} LLM calls on the partial-tool-call "
                "path — the recovery retry is unbounded",
            )
        return _partial_tool_call_response()

    monkeypatch.setattr(
        "surogates.harness.loop.call_llm_with_retry", always_partial,
    )

    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())

    await asyncio.wait_for(
        harness._run_loop(
            session,
            [{"role": "user", "content": "write the file"}],
            "system",
            lease,
            all_events=[],
        ),
        timeout=10.0,
    )

    assert calls <= _RUNAWAY_THRESHOLD, "recovery retry is unbounded"


@pytest.mark.asyncio
async def test_partial_tool_call_exhaustion_ends_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the retries are spent the turn ends with a final response.

    The session must not be left mid-turn with nothing emitted — the user
    is owed an answer explaining why the work stopped.
    """
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 100 + 4 * _RUNAWAY_THRESHOLD))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(
        session_store=store, budget=IterationBudget(max_total=50),
    )

    calls = 0

    async def always_partial(**_kwargs: Any) -> tuple[dict, dict]:
        nonlocal calls
        calls += 1
        if calls > _RUNAWAY_THRESHOLD:
            raise AssertionError("recovery retry is unbounded")
        return _partial_tool_call_response()

    monkeypatch.setattr(
        "surogates.harness.loop.call_llm_with_retry", always_partial,
    )
    harness._request_final_summary = AsyncMock(return_value=None)

    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())

    await asyncio.wait_for(
        harness._run_loop(
            session,
            [{"role": "user", "content": "write the file"}],
            "system",
            lease,
            all_events=[],
        ),
        timeout=10.0,
    )

    assert harness._request_final_summary.await_count == 1, (
        "exhausting the partial-tool-call retries must end the turn with a "
        "final summary, not fall through to tool execution"
    )
    # The budget is never the thing that stopped it — the retry cap is.
    assert harness._budget.remaining > 0


@pytest.mark.asyncio
async def test_partial_tool_call_does_not_clear_the_invalid_json_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider alternating the two malformed-args failures must terminate.

    The partial-tool-call branch used to reset ``invalid_json_retries``, so a
    provider that alternated truncated arguments with unparseable arguments
    kept each counter below its own cap indefinitely.
    """
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 100 + 4 * _RUNAWAY_THRESHOLD))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(
        session_store=store, budget=IterationBudget(max_total=50),
    )

    calls = 0

    async def alternating(**_kwargs: Any) -> tuple[dict, dict]:
        nonlocal calls
        calls += 1
        if calls > _RUNAWAY_THRESHOLD:
            raise AssertionError(
                f"_run_loop made {calls} LLM calls alternating truncated and "
                "unparseable tool arguments — the two counters reset each other",
            )
        if calls % 2:
            return _partial_tool_call_response()
        return (
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{calls}",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": "not json"},
                    }
                ],
            },
            {
                "model": "test-model",
                "finish_reason": "tool_calls",
                "input_tokens": 1,
                "output_tokens": 1,
            },
        )

    monkeypatch.setattr(
        "surogates.harness.loop.call_llm_with_retry", alternating,
    )
    harness._request_final_summary = AsyncMock(return_value=None)

    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())

    await asyncio.wait_for(
        harness._run_loop(
            session,
            [{"role": "user", "content": "write the file"}],
            "system",
            lease,
            all_events=[],
        ),
        timeout=10.0,
    )

    assert calls <= _RUNAWAY_THRESHOLD


@pytest.mark.asyncio
async def test_one_partial_tool_call_still_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap must not break the recovery it bounds.

    A single truncated response is a transient provider hiccup: the loop
    still refunds the iteration, hands the model a recovery tool result and
    carries on to a successful turn.
    """
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])

    harness = _make_loop_harness(
        session_store=store, budget=IterationBudget(max_total=5),
    )

    calls = 0

    async def partial_then_ok(**_kwargs: Any) -> tuple[dict, dict]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _partial_tool_call_response()
        return (
            {"role": "assistant", "content": "Wrote the file.", "tool_calls": None},
            {"model": "test-model", "finish_reason": "stop",
             "input_tokens": 1, "output_tokens": 2},
        )

    monkeypatch.setattr(
        "surogates.harness.loop.call_llm_with_retry", partial_then_ok,
    )

    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())

    emits: list[Any] = []
    await asyncio.wait_for(
        harness._run_loop(
            session,
            [{"role": "user", "content": "write the file"}],
            "system",
            lease,
            all_events=[],
        ),
        timeout=10.0,
    )
    emits = [c.args[1] for c in store.emit_event.await_args_list]

    assert calls == 2, "the truncated response must be retried, not fatal"
    assert EventType.LLM_RESPONSE in emits
    # The refund kept the retry off the budget.
    assert harness._budget.used == 1
