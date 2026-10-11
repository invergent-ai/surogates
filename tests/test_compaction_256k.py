"""Compaction on a 256k-window model, driven through whole agent runs.

The harness checks for compaction after every tool round, using the prompt
size the provider reported for the call that just ran.  These tests replay
that loop against the real ``ContextCompressor`` with tool results sized like
those of a PROD research session, and check after every compaction what a
session depends on: the task and the newest tool result survive, tool calls
keep their results, roles alternate, exactly one handoff summary is in
context, the prompt never outgrows the window, and the summary of earlier
compactions is carried forward.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from surogates.harness import context as context_module
from surogates.harness.context import (
    NOOP_STRATEGIES,
    SUMMARY_PREFIX,
    ContextCompressor,
    _estimate_messages_tokens_rough,
)

WINDOW = 262_144  # surogate-pro
OUTPUT_RESERVE = 16_384  # max_tokens the harness asks for on every main call
OVERHEAD = 30_000  # system prompt + tool schemas: in every prompt, never compacted
TASK = "Research CVE-2021-44228 and write up the CSRB recommendations."
SECONDS_PER_ROUND = 10.0

# Tool results by size in chars, as a PROD research session produced them.
PROD_RESULTS = [
    ("web_search", 100), ("web_fetch", 50_000), ("browser_navigate", 9_000),
    ("web_fetch", 8_000), ("browser_evaluate", 150), ("web_fetch", 5_300),
    ("skill_view", 23_000), ("terminal", 3_000), ("web_fetch", 7_000),
]
SMALL_RESULTS = [("web_search", 400)]


def _compressor(**kwargs: Any) -> ContextCompressor:
    return ContextCompressor(
        "surogate-pro", quiet_mode=True,
        model_overrides={"surogate-pro": {"context_window": WINDOW}}, **kwargs,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """The compressor's clock, moved by the run instead of by wall time."""
    fake = _Clock()
    monkeypatch.setattr(context_module, "time", fake)
    return fake


class _Summariser:
    """An OpenAI-shaped client whose summary names the call that wrote it."""

    def __init__(self, *, failing: frozenset[int] = frozenset()) -> None:
        self.prompts: list[str] = []
        self._failing = failing
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=self._create)),
        )

    async def _create(self, **kwargs: Any) -> Any:
        self.prompts.append(kwargs["messages"][0]["content"])
        call = len(self.prompts)
        if call in self._failing:
            raise RuntimeError("summariser unavailable")
        body = f"## Goal\nCHECKPOINT-{call}\n## Findings\n" + "finding " * 300
        message = SimpleNamespace(content=body, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=message)])


_TURNS = re.compile(
    r"(?:TURNS TO SUMMARIZE|NEW TURNS TO INCORPORATE):\n(.*?)\n\n"
    r"(?:Use this exact structure|Update the summary)",
    re.S,
)


def _turns_in(prompt: str) -> str:
    match = _TURNS.search(prompt)
    assert match, "summary prompt without a turns section"
    return match.group(1)


@dataclass
class _Run:
    prompts: list[int] = field(default_factory=list)
    compactions: list[tuple[int, str]] = field(default_factory=list)  # (round, strategy)
    summaries: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)


async def _run_agent(
    summariser: _Summariser,
    *,
    rounds: int,
    overhead: int = OVERHEAD,
    results: list[tuple[str, int]] = PROD_RESULTS,
    calls_per_round: int = 2,
    history: list[dict[str, Any]] | None = None,
    clock: _Clock | None = None,
    new_wake_every_round: bool = False,
) -> _Run:
    """Replay the harness loop: call, tool round, compaction check."""
    comp = _compressor()
    sizes = itertools.cycle(results)
    messages = list(history) if history else [{"role": "user", "content": TASK}]
    run = _Run()
    for index in range(rounds):
        if new_wake_every_round:
            comp = _compressor()  # harness_factory builds one per wake
        prompt = overhead + _estimate_messages_tokens_rough(messages)
        run.prompts.append(prompt)
        calls = []
        for k in range(calls_per_round):
            name, size = next(sizes)
            calls.append((f"call_{index}_{k}", name, size))
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps({"q": cid})}}
            for cid, name, _ in calls
        ]})
        for cid, name, size in calls:
            messages.append({"role": "tool", "tool_call_id": cid,
                             "content": f"[{name} {cid}] " + "r" * size})
        if comp.should_compress(prompt):
            compressed, data = await comp.compress(
                messages, summariser.client, current_tokens=prompt,
            )
            run.compactions.append((index, data["strategy"]))
            if data["strategy"] not in NOOP_STRATEGIES:
                _assert_well_formed(messages, compressed)
                run.summaries.append(data.get("summary") or "")
                messages = compressed
        if clock is not None:
            clock.now += SECONDS_PER_ROUND
    run.messages = messages
    return run


def _assert_tool_pairs(messages: list[dict[str, Any]]) -> None:
    """Every call is answered right after it, every result has its call."""
    pending: set[str] = set()
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "tool":
            assert msg["tool_call_id"] in pending, f"result at {i} has no call"
            pending.discard(msg["tool_call_id"])
            continue
        assert not pending, f"calls {pending} unanswered before message {i}"
        if role == "assistant":
            pending = {tc["id"] for tc in msg.get("tool_calls") or []}
    assert not pending, f"calls {pending} unanswered at the end"


def _assert_roles_alternate(messages: list[dict[str, Any]]) -> None:
    """No two user or two assistant messages side by side."""
    for i, (a, b) in enumerate(zip(messages, messages[1:])):
        assert not (a["role"] == b["role"] and a["role"] in ("user", "assistant")), (
            f"two {a['role']} messages in a row at {i}"
        )


def _assert_well_formed(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    assert len(after) < len(before)
    assert after[0] == before[0], "the task must stay first, word for word"
    assert after[-1]["tool_call_id"] == before[-1]["tool_call_id"], (
        "the round the model reads next must not be summarised away"
    )
    summaries = [m for m in after if SUMMARY_PREFIX in str(m.get("content"))]
    assert len(summaries) == 1, f"{len(summaries)} handoff summaries in context"
    _assert_tool_pairs(after)
    _assert_roles_alternate(after)


# -- budgets -----------------------------------------------------------------


def test_a_256k_window_compacts_at_three_quarters_of_it() -> None:
    comp = _compressor()
    assert comp.threshold_tokens == 196_608
    assert comp.tail_token_budget == int(196_608 * 0.20)
    assert comp.max_summary_tokens == 12_000  # the ceiling, under 5% of the window


def test_the_old_half_window_trigger_no_longer_compacts_at_256k() -> None:
    assert not _compressor().should_compress(WINDOW // 2)
    assert not _compressor().should_compress(196_607)
    assert _compressor().should_compress(196_608)


def test_a_configured_threshold_above_three_quarters_is_kept_at_256k() -> None:
    assert _compressor(threshold_percent=0.85).threshold_tokens == int(WINDOW * 0.85)


def test_a_provider_rejection_shrinks_every_256k_budget() -> None:
    comp = _compressor()
    assert comp.set_context_length(200_000)
    assert comp.threshold_tokens == 150_000
    assert comp.tail_token_budget == 30_000
    assert comp.max_summary_tokens == 10_000


def test_the_trigger_leaves_room_for_the_reply_and_a_round_of_large_results() -> None:
    """Compaction runs after the round that crossed the trigger, so that
    round's results ride on a prompt already at the trigger."""
    largest_round = 2 * max(size for _, size in PROD_RESULTS) // 4
    assert _compressor().threshold_tokens + largest_round + OUTPUT_RESERVE <= WINDOW


# -- the protected tail ----------------------------------------------------


def _alternating(count: int, chars: int, first: str = "user") -> list[dict[str, Any]]:
    other = "assistant" if first == "user" else "user"
    return [
        {"role": first if i % 2 == 0 else other, "content": "m" * chars}
        for i in range(count)
    ]


def test_a_tail_that_fits_its_budget_keeps_more_than_the_floor() -> None:
    comp = _compressor()
    messages = [{"role": "user", "content": TASK}, *_alternating(40, 8_000, "assistant")]
    per_message = 8_000 // 4 + 10

    cut = comp._find_tail_cut_by_tokens(messages, 1)

    assert len(messages) - cut == comp.tail_token_budget // per_message
    assert _estimate_messages_tokens_rough(messages[cut:]) <= comp.tail_token_budget


def test_a_bulky_tail_is_cut_to_eight_messages() -> None:
    comp = _compressor()
    messages = [{"role": "user", "content": TASK}, *_alternating(30, 100_000, "assistant")]

    cut = comp._find_tail_cut_by_tokens(messages, 1)

    assert len(messages) - cut == 8


@pytest.mark.parametrize("result_chars", range(2_000, 60_001, 2_000))
def test_the_tail_never_splits_a_round_of_parallel_tool_calls(result_chars: int) -> None:
    """Wherever the budget boundary falls, a round stays whole on one side."""
    comp = _compressor()
    messages: list[dict[str, Any]] = [{"role": "user", "content": TASK}]
    for index in range(12):
        ids = [f"c{index}_{k}" for k in range(3)]
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "web_fetch", "arguments": "{}"}}
            for cid in ids
        ]})
        messages.extend(
            {"role": "tool", "tool_call_id": cid, "content": "r" * result_chars} for cid in ids
        )

    cut = comp._find_tail_cut_by_tokens(messages, 1)

    assert messages[cut]["role"] == "assistant"
    _assert_tool_pairs(messages[cut:])


# -- whole runs ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_long_research_run_compacts_rarely_and_stays_well_formed() -> None:
    summariser = _Summariser()
    comp_threshold = _compressor().threshold_tokens

    run = await _run_agent(summariser, rounds=200)

    assert all(p + OUTPUT_RESERVE <= WINDOW for p in run.prompts), "a prompt overflowed"
    rounds = [index for index, _ in run.compactions]
    assert len(rounds) >= 3, "the run must be long enough to need compaction"
    assert {s for _, s in run.compactions} == {"summarise"}
    for index in rounds:
        assert run.prompts[index] >= comp_threshold
        assert run.prompts[index + 1] < comp_threshold, (
            f"compaction at round {index} left the prompt over the trigger"
        )
    # Each compaction frees most of the window: the next is many rounds away.
    assert min(b - a for a, b in zip(rounds, rounds[1:])) >= 10
    assert len(summariser.prompts) == len(rounds)
    assert all(_turns_in(p).strip() for p in summariser.prompts)
    for call, prompt in enumerate(summariser.prompts[1:], start=1):
        assert f"CHECKPOINT-{call}" in prompt, "the previous summary was not carried"
    assert run.messages[0]["content"] == TASK


@pytest.mark.asyncio
async def test_a_compressor_rebuilt_by_every_wake_carries_the_summary_forward() -> None:
    summariser = _Summariser()

    run = await _run_agent(summariser, rounds=120, new_wake_every_round=True)

    assert len(summariser.prompts) >= 2
    for call, prompt in enumerate(summariser.prompts[1:], start=1):
        assert "PREVIOUS SUMMARY" in prompt
        assert f"CHECKPOINT-{call}" in prompt
    assert all(_turns_in(p).strip() for p in summariser.prompts)


@pytest.mark.asyncio
async def test_a_summariser_outage_mid_run_never_loses_the_last_checkpoint(
    clock: _Clock,
) -> None:
    summariser = _Summariser(failing=frozenset({2}))

    run = await _run_agent(summariser, rounds=260, clock=clock)

    rounds = [index for index, _ in run.compactions]
    strategies = [s for _, s in run.compactions]
    first_failure = strategies.index("summary_unavailable")
    recovered = strategies.index("summarise", first_failure)
    assert strategies[:first_failure] == ["summarise"]  # the failing call is the second
    # The summariser is not asked again until its cooldown has passed.
    cooldown_rounds = context_module._SUMMARY_FAILURE_COOLDOWN_SECONDS / SECONDS_PER_ROUND
    assert recovered - first_failure >= 2, "the run must compact during the cooldown"
    assert all(r < rounds[first_failure] + cooldown_rounds for r in rounds[first_failure:recovered])
    assert rounds[recovered] >= rounds[first_failure] + cooldown_rounds
    # Every handoff written while it was down still holds the last checkpoint.
    for summary in run.summaries[first_failure:recovered]:
        assert "CHECKPOINT-1" in summary
        assert "could not be summarised" in summary
    # And the next summary is built from it, gap noted.
    assert "CHECKPOINT-1" in summariser.prompts[2]
    assert "could not be summarised" in summariser.prompts[2]
    assert "CHECKPOINT-3" in run.summaries[recovered]


@pytest.mark.asyncio
async def test_an_incompressible_prompt_costs_at_most_one_summary_per_pause(
    clock: _Clock,
) -> None:
    """System prompt and tool schemas alone over the trigger: compaction
    cannot help, so it must not run every round."""
    summariser = _Summariser()
    history: list[dict[str, Any]] = [{"role": "user", "content": TASK}]
    for index in range(40):
        history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"h{index}", "type": "function",
             "function": {"name": "web_search", "arguments": "{}"}},
        ]})
        history.append({"role": "tool", "tool_call_id": f"h{index}", "content": "x" * 400})
    rounds = 120

    run = await _run_agent(
        summariser, rounds=rounds, overhead=200_000, results=SMALL_RESULTS,
        history=history, clock=clock,
    )

    assert all(p >= _compressor().threshold_tokens for p in run.prompts)
    assert all(p + OUTPUT_RESERVE <= WINDOW for p in run.prompts)
    pause_windows = rounds * SECONDS_PER_ROUND / 300
    assert len(summariser.prompts) <= pause_windows + 1
    assert len(run.compactions) <= 2 * (pause_windows + 1)
    # Once paused, one summary per pause, not one per round.
    summarised = [index for index, strategy in run.compactions if strategy == "summarise"]
    assert len(summarised) >= 3, "the run must outlast several pauses"
    pause_rounds = 300 / SECONDS_PER_ROUND
    assert all(b - a >= pause_rounds for a, b in zip(summarised[1:], summarised[2:]))


@pytest.mark.asyncio
async def test_a_user_message_merged_into_the_summary_survives_the_next_compaction() -> None:
    """When neither role alternates, the summary is merged into the first
    tail message.  That message's own text is live conversation, not part of
    the summary, and must not vanish at the next compaction."""
    summariser = _Summariser()
    comp = _compressor()
    ask = "ALSO-COVER: CVE-2021-45046, the incomplete fix"
    head = [
        {"role": "user", "content": TASK},
        {"role": "user", "content": "attached: csrb-report.pdf"},
        {"role": "assistant", "content": "on it"},
    ]
    tail = [{"role": "user", "content": ask}, *_alternating(7, 100, "assistant")]
    messages = head + _alternating(20, 100) + tail

    first, data = await comp.compress(messages, summariser.client)
    assert data["strategy"] == "summarise"
    merged = [m for m in first if ask in str(m.get("content"))]
    assert merged and SUMMARY_PREFIX in merged[0]["content"]

    second, data = await comp.compress(first + _alternating(20, 100), summariser.client)

    assert data["strategy"] == "summarise"
    assert ask in _turns_in(summariser.prompts[1]) or any(
        ask in str(m.get("content")) for m in second
    ), "the user's request was dropped"
