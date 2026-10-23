"""Compaction must not run again and again without freeing context.

A PROD session (Surogate3.7-35B-A3B, 128k window) compacted 15 times in 13
minutes, each pass a blocking summariser call of 5-156 s.  The trigger sat at
64k while the part of the prompt compaction may not touch -- system prompt and
tool schemas (~20k), the protected head and a 20-message tail -- sat just under
it, so every pass summarised 0-5 messages and the next tool result crossed the
line again.  Passes with nothing to summarise still called the summariser, and a
reasoning summariser capped at 4000 output tokens returned empty or cut-off
summaries that replaced the previous one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from surogates.harness import context as context_module
from surogates.harness.context import SUMMARY_PREFIX, ContextCompressor
from surogates.session.events import EventType
from tests.test_loop_ordering import _drive, _harness, _resp, _tool_resp
from tests.test_steer_loop import _make_loop_harness, _make_session


def _compressor(**kwargs: Any) -> ContextCompressor:
    return ContextCompressor("gpt-4o-mini", quiet_mode=True, **kwargs)  # 128k window


def _head() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "kick off the work"},
        {"role": "assistant", "content": "starting"},
        {"role": "user", "content": "carry on"},
    ]


def _conversation(n: int) -> list[dict[str, Any]]:
    messages = _head()
    for i in range(n):
        messages.append({"role": "assistant", "content": f"step {i} " + "x" * 200})
        messages.append({"role": "user", "content": f"next {i}"})
    return messages


def _with_previous_summary(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A prior compaction's marker just past the protected head."""
    marker = {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nearlier work: built the parser"}
    return [*messages[:3], marker, *messages[3:]]


def _summarising(comp: ContextCompressor, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    async def fake_generate(turns, client, **kw):
        calls.append(turns)
        return f"{SUMMARY_PREFIX}\nrolled up"

    monkeypatch.setattr(comp, "_generate_summary", fake_generate)
    return calls


def _five_minutes_later(monkeypatch: pytest.MonkeyPatch) -> None:
    """Move the compressor's clock past a pause without touching asyncio's."""
    later = context_module.time.monotonic() + 301
    monkeypatch.setattr(context_module, "time", SimpleNamespace(monotonic=lambda: later))


def _client(content: str = "## Goal\ndone", *, finish: str = "stop",
            refusal: str | None = None, error: Exception | None = None,
            seen: list[dict] | None = None) -> Any:
    async def create(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        if error is not None:
            raise error
        message = SimpleNamespace(content=content, refusal=refusal)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=message)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


# -- trigger ---------------------------------------------------------------


def test_a_window_under_512k_compacts_at_three_quarters() -> None:
    assert _compressor().threshold_tokens == 96_000


def test_a_large_window_compacts_at_nine_tenths_until_it_shrinks() -> None:
    comp = ContextCompressor(
        "big", quiet_mode=True, model_overrides={"big": {"context_window": 1_000_000}},
    )
    assert comp.threshold_tokens == 900_000
    comp.set_context_length(400_000)
    assert comp.threshold_tokens == 300_000


# -- protected tail --------------------------------------------------------


def test_the_tail_keeps_at_most_eight_messages_once_over_its_token_budget() -> None:
    comp = _compressor()
    bulky = "x" * (comp.tail_token_budget // 2 * 4)  # half the tail budget each
    messages = _head() + [
        {"role": "assistant" if i % 2 == 0 else "user", "content": bulky}
        for i in range(30)
    ]

    cut = comp._find_tail_cut_by_tokens(messages, 3)

    assert len(messages) - cut == 8


# -- nothing to summarise ----------------------------------------------------


def _only_previous_summary_in_window(comp: ContextCompressor) -> list[dict[str, Any]]:
    """The previous summary alone outweighs the tail budget, so it is the
    whole window between the protected head and the protected tail."""
    marker = {
        "role": "assistant",
        "content": f"{SUMMARY_PREFIX}\n" + "s" * (comp.tail_token_budget * 4),
    }
    tail = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
        for i in range(22)
    ]
    return _head() + [marker] + tail


@pytest.mark.asyncio
async def test_a_window_holding_only_the_previous_summary_skips_the_summariser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp = _compressor()
    calls = _summarising(comp, monkeypatch)
    messages = _only_previous_summary_in_window(comp)

    compressed, data = await comp.compress(messages, llm_client=object())

    assert calls == []
    assert compressed == messages
    assert data["strategy"] == "nothing_to_summarise"


@pytest.mark.asyncio
async def test_after_nothing_to_summarise_compaction_waits_before_trying_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp = _compressor()
    _summarising(comp, monkeypatch)
    await comp.compress(_only_previous_summary_in_window(comp), llm_client=object())

    assert not comp.should_compress(comp.threshold_tokens)

    _five_minutes_later(monkeypatch)
    assert comp.should_compress(comp.threshold_tokens)


# -- compactions that do not free enough -------------------------------------


@pytest.mark.asyncio
async def test_two_compactions_that_leave_the_prompt_over_the_threshold_block_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp = _compressor()
    _summarising(comp, monkeypatch)
    over = comp.threshold_tokens + 1

    assert comp.should_compress(over)
    await comp.compress(_conversation(40), llm_client=object())
    assert comp.should_compress(over)  # one strike: worth another try
    await comp.compress(_conversation(40), llm_client=object())

    assert not comp.should_compress(over)


@pytest.mark.asyncio
async def test_a_compaction_that_clears_the_threshold_forgives_an_earlier_strike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp = _compressor()
    _summarising(comp, monkeypatch)
    over, under = comp.threshold_tokens + 1, comp.threshold_tokens - 1

    await comp.compress(_conversation(40), llm_client=object())
    assert comp.should_compress(over)
    await comp.compress(_conversation(40), llm_client=object())
    assert not comp.should_compress(under)
    await comp.compress(_conversation(40), llm_client=object())

    assert comp.should_compress(over)


@pytest.mark.asyncio
async def test_a_blocked_compressor_probes_once_when_the_block_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp = _compressor()
    _summarising(comp, monkeypatch)
    over = comp.threshold_tokens + 1
    for _ in range(2):
        await comp.compress(_conversation(40), llm_client=object())
        comp.should_compress(over)

    _five_minutes_later(monkeypatch)
    assert comp.should_compress(over)
    await comp.compress(_conversation(40), llm_client=object())

    assert not comp.should_compress(over)


# -- unusable summaries ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_summary_call_sets_no_output_cap() -> None:
    """A reasoning summariser spends a capped budget thinking and returns nothing."""
    comp = _compressor()
    seen: list[dict] = []

    await comp.compress(_conversation(40), llm_client=_client(seen=seen))

    assert seen and "max_tokens" not in seen[0]


@pytest.mark.parametrize(
    "client",
    [
        _client(""),
        _client("## Goal\nResearch the CVE and", finish="length"),
        _client("I can't help with that.", refusal="I can't help with that."),
        _client(error=RuntimeError("summariser down")),
    ],
    ids=["empty", "cut off", "refused", "error"],
)
@pytest.mark.asyncio
async def test_an_unusable_summary_keeps_the_previous_one(client: Any) -> None:
    comp = _compressor()
    messages = _with_previous_summary(_conversation(40))

    compressed, data = await comp.compress(messages, llm_client=client)

    assert data["strategy"] == "summary_unavailable"
    assert any("built the parser" in str(m.get("content")) for m in compressed)
    assert "built the parser" in (comp._previous_summary or "")


# -- the loop ---------------------------------------------------------------


def _changing_nothing(harness: Any) -> None:
    harness._compressor = SimpleNamespace(
        context_length=1000,
        should_compress=lambda *a, **k: True,
        compress=AsyncMock(side_effect=lambda messages, *a, **k: (list(messages), {
            "strategy": "nothing_to_summarise",
            "original_token_estimate": 1, "compressed_token_estimate": 1,
        })),
    )
    harness._memory_snapshot_cache = {}


@pytest.mark.asyncio
async def test_a_compaction_in_the_loop_that_changed_nothing_writes_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness()
    _changing_nothing(harness)

    emits = await _drive(harness, [_tool_resp("c1"), _resp("Done.")], monkeypatch)

    harness._compressor.compress.assert_awaited()
    assert EventType.CONTEXT_COMPACT not in [etype for etype, _ in emits]


@pytest.mark.asyncio
async def test_a_compaction_at_wake_that_changed_nothing_writes_no_event() -> None:
    harness = _make_loop_harness(session_store=AsyncMock())
    _changing_nothing(harness)
    harness._prompt = SimpleNamespace(build=lambda: "system")
    history = _conversation(4)

    assert await harness._engineer_context(_make_session(), [], history) == history

    harness._compressor.compress.assert_awaited()
    assert not harness._store.emit_event.await_count
