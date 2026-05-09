"""Tests for stateful stream scrubbers."""

from __future__ import annotations

from surogates.harness.stream_scrubbers import (
    StreamingContextScrubber,
    StreamingThinkScrubber,
)


def _feed_all(scrubber, chunks: list[str]) -> str:
    visible = [scrubber.feed(chunk) for chunk in chunks]
    visible.append(scrubber.flush())
    return "".join(visible)


def test_think_scrubber_removes_split_reasoning_block() -> None:
    scrubber = StreamingThinkScrubber()

    visible = _feed_all(
        scrubber,
        ["Hello\n<th", "ink>private reasoning", "</think>\nFinal"],
    )

    assert visible == "Hello\n\nFinal"
    assert "private reasoning" not in visible


def test_think_scrubber_handles_multiple_reasoning_tag_variants() -> None:
    for tag in ("thinking", "reasoning", "thought", "REASONING_SCRATCHPAD"):
        scrubber = StreamingThinkScrubber()
        visible = _feed_all(scrubber, [f"<{tag}>secret", f"</{tag}>answer"])
        assert visible == "answer"


def test_think_scrubber_keeps_mid_sentence_tag_mentions() -> None:
    scrubber = StreamingThinkScrubber()

    visible = _feed_all(scrubber, ["Please write about <think> tags, not reasoning."])

    assert visible == "Please write about <think> tags, not reasoning."


def test_think_scrubber_discards_unclosed_reasoning_on_flush() -> None:
    scrubber = StreamingThinkScrubber()

    visible = _feed_all(scrubber, ["Intro\n<thinking>private reasoning"])

    assert visible == "Intro\n"
    assert "private reasoning" not in visible


def test_context_scrubber_removes_split_memory_context_span() -> None:
    scrubber = StreamingContextScrubber()

    visible = _feed_all(
        scrubber,
        [
            "Before <memory-",
            "context>\n[System note: x]\nsecret memory",
            "</memory-context> after",
        ],
    )

    assert visible == "Before  after"
    assert "secret memory" not in visible
    assert "System note" not in visible


def test_context_scrubber_flushes_false_partial_tag() -> None:
    scrubber = StreamingContextScrubber()

    visible = _feed_all(scrubber, ["Use <memory as a word"])

    assert visible == "Use <memory as a word"
