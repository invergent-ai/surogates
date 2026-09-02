"""Per-wake work that used to be repeated per iteration, and the helpers
the loop had grown a second copy of."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.session.events import EventType
from tests.test_loop_ordering import _drive, _harness, _resp, _tool_resp
from tests.test_steer_loop import _make_loop_harness, _make_session


def _registry(*names: str) -> MagicMock:
    reg = MagicMock()
    reg.tool_names = set(names)
    reg.get_schemas = MagicMock(return_value=[
        {"type": "function", "function": {"name": n}} for n in names
    ])
    reg.get_all = MagicMock(return_value=[
        SimpleNamespace(name=n, toolset="core") for n in names
    ])
    return reg


# -- per-wake work ----------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_schemas_are_built_once_per_wake(monkeypatch):
    h = _harness()
    h._tools = _registry("noop", "read_file")
    await _drive(h, [_tool_resp("c1"), _tool_resp("c2"), _resp("Done.")], monkeypatch)
    assert h._tools.get_schemas.call_count == 1


@pytest.mark.asyncio
async def test_browser_pause_is_not_polled_without_browser_tools(monkeypatch):
    held_by = AsyncMock(return_value=None)
    h = _harness(_browser_control=SimpleNamespace(held_by=held_by))
    h._tools = _registry("noop", "read_file")
    await _drive(h, [_tool_resp("c1"), _resp("Done.")], monkeypatch)
    held_by.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_pause_is_polled_when_the_session_has_browser_tools(monkeypatch):
    held_by = AsyncMock(return_value=None)
    h = _harness(_browser_control=SimpleNamespace(held_by=held_by))
    h._tools = _registry("noop", "browser_navigate")
    await _drive(h, [_resp("Done.")], monkeypatch)
    held_by.assert_awaited()


@pytest.mark.asyncio
async def test_compression_check_uses_the_reported_prompt_tokens(monkeypatch):
    seen: list[Any] = []
    h = _harness()
    h._compressor = SimpleNamespace(
        context_length=1000,
        _context_window=200_000,
        should_compress=lambda m, *a, **k: (seen.append(m), False)[1],
    )
    await _drive(h, [_tool_resp("c1"), _resp("Done.")], monkeypatch)
    assert seen == [1], seen


# -- final-summary bookkeeping ---------------------------------------------


@pytest.mark.asyncio
async def test_final_summary_payload_matches_the_main_loop(monkeypatch):
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(900, 1000))
    h = _make_loop_harness(session_store=store, budget=IterationBudget(max_total=1))
    h._warn_unpriced_model_once = MagicMock()
    tracker = MagicMock()

    async def fake_call(**_k):
        return (
            {"role": "assistant", "content": "Summary."},
            {"model": "unpriced-model", "finish_reason": "stop",
             "input_tokens": 7, "output_tokens": 5, "reasoning_tokens": 3,
             "cache_read_tokens": 2},
        )

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call)

    await h._request_final_summary(
        _make_session(), [{"role": "user", "content": "do it"}], "system",
        SimpleNamespace(lease_token=uuid4()),
        cost_tracker=tracker, turn_id="turn-final",
    )

    payload = next(
        c.args[2] for c in store.emit_event.await_args_list
        if c.args[1] == EventType.LLM_RESPONSE
    )
    assert payload["reasoning_tokens"] == 3
    assert payload["context_window"] == h._compressor.context_length
    # No catalog rate for the serving model, so the gap is recorded rather
    # than the session silently reading as free.
    assert payload["cost_unpriced_model"] == "unpriced-model"
    h._warn_unpriced_model_once.assert_called_once_with("unpriced-model")
    assert tracker.record_call.call_args.kwargs["reasoning_tokens"] == 3


# -- one flattener ----------------------------------------------------------


def test_content_as_text_flattens_parts():
    from surogates.harness.message_utils import content_as_text

    assert content_as_text("plain") == "plain"
    assert content_as_text(None) == ""
    assert content_as_text([{"type": "text", "text": "a"}, "b"]) == "ab"
    # A part with no text of its own (an image) contributes nothing.
    assert content_as_text([
        {"type": "text", "text": "a"},
        {"type": "image_url", "image_url": {"url": "data:..."}},
    ]) == "a"
    assert content_as_text(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], sep="\n",
    ) == "a\nb"


def test_the_loop_and_llm_call_share_one_flattener():
    from surogates.harness import llm_call, title_generator
    from surogates.harness.message_utils import content_as_text

    assert llm_call._message_content_as_text is content_as_text
    assert title_generator._content_as_text is content_as_text
