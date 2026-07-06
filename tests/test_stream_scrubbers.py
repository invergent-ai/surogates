"""Tests for stateful stream scrubbers."""

from __future__ import annotations

from surogates.harness.stream_scrubbers import (
    UPSTREAM_ERROR_SENTINELS,
    StreamingContextScrubber,
    StreamingSentinelScrubber,
    StreamingThinkScrubber,
    is_upstream_error_sentinel,
)

_SENTINEL = UPSTREAM_ERROR_SENTINELS[0]


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


# ---------------------------------------------------------------------------
# StreamingSentinelScrubber — suppress upstream-gateway error strings
# ---------------------------------------------------------------------------


def test_sentinel_scrubber_suppresses_whole_string_single_chunk() -> None:
    scrubber = StreamingSentinelScrubber()

    visible = _feed_all(scrubber, [_SENTINEL])

    assert visible == ""
    assert scrubber.matched is True


def test_sentinel_scrubber_suppresses_string_split_across_chunks() -> None:
    scrubber = StreamingSentinelScrubber()
    third = len(_SENTINEL) // 3
    chunks = [_SENTINEL[:third], _SENTINEL[third : 2 * third], _SENTINEL[2 * third :]]

    visible = _feed_all(scrubber, chunks)

    assert visible == ""
    assert scrubber.matched is True


def test_sentinel_scrubber_suppresses_char_by_char() -> None:
    scrubber = StreamingSentinelScrubber()

    visible = _feed_all(scrubber, list(_SENTINEL))

    assert visible == ""
    assert scrubber.matched is True


def test_sentinel_scrubber_passes_normal_text_untouched() -> None:
    scrubber = StreamingSentinelScrubber()

    visible = _feed_all(scrubber, ["Here is ", "your answer."])

    assert visible == "Here is your answer."
    assert scrubber.matched is False


def test_sentinel_scrubber_keeps_legit_warning_sharing_emoji_prefix() -> None:
    scrubber = StreamingSentinelScrubber()
    legit = "⚠️ Note: the disk is almost full, consider cleanup."

    visible = _feed_all(scrubber, [legit])

    assert visible == legit
    assert scrubber.matched is False


def test_sentinel_scrubber_keeps_legit_warning_split_across_chunks() -> None:
    scrubber = StreamingSentinelScrubber()
    # Shares "⚠️ " with the sentinel, then diverges on the next character.
    visible = _feed_all(scrubber, ["⚠️", " Warn", "ing: low battery"])

    assert visible == "⚠️ Warning: low battery"
    assert scrubber.matched is False


def test_sentinel_scrubber_flushes_lone_emoji() -> None:
    scrubber = StreamingSentinelScrubber()

    visible = _feed_all(scrubber, ["⚠️"])

    # A lone shared lead-in has no CJK -> it is legitimate output.
    assert visible == "⚠️"
    assert scrubber.matched is False


def test_sentinel_scrubber_suppresses_truncated_sentinel_on_flush() -> None:
    scrubber = StreamingSentinelScrubber()
    # Stream dies partway through the sentinel — still a gateway error.
    partial = _SENTINEL[: len(_SENTINEL) // 2]

    visible = _feed_all(scrubber, [partial])

    assert visible == ""
    assert scrubber.matched is True


def test_sentinel_scrubber_suppresses_short_truncated_cjk_prefix() -> None:
    # Regression: a stream cut just past the emoji into the first CJK chars
    # (e.g. "⚠️ 上游模") must NOT leak the partial Chinese to the user.
    scrubber = StreamingSentinelScrubber()

    visible = _feed_all(scrubber, ["⚠️ 上游模"])

    assert visible == ""
    assert "上游" not in visible
    assert scrubber.matched is True


def test_sentinel_scrubber_emits_short_non_cjk_prefix() -> None:
    # "⚠️ " (emoji + space) shares the lead-in but has no CJK -> legitimate.
    scrubber = StreamingSentinelScrubber()

    visible = _feed_all(scrubber, ["⚠️ "])

    assert visible == "⚠️ "
    assert scrubber.matched is False


def test_sentinel_scrubber_emits_text_following_divergence() -> None:
    scrubber = StreamingSentinelScrubber()
    # Diverges from the sentinel after "⚠️ 上游" -> everything must be emitted.
    text = "⚠️ 上游 status: all systems normal."

    visible = _feed_all(scrubber, [text])

    assert visible == text
    assert scrubber.matched is False


def test_is_upstream_error_sentinel_matches_full_and_marker() -> None:
    assert is_upstream_error_sentinel(_SENTINEL) is True
    assert is_upstream_error_sentinel("  " + _SENTINEL + "\n") is True
    # Distinctive marker embedded in a slightly different wrapper.
    assert is_upstream_error_sentinel("提示：上游模型未返回任何内容，请重试") is True


def test_is_upstream_error_sentinel_ignores_legit_text() -> None:
    assert is_upstream_error_sentinel("") is False
    assert is_upstream_error_sentinel(None) is False
    assert is_upstream_error_sentinel("Here is your answer.") is False
    assert is_upstream_error_sentinel("⚠️ Note: disk almost full") is False


def test_contains_cjk_detects_sentinel_body_not_emoji() -> None:
    from surogates.harness.stream_scrubbers import _contains_cjk

    assert _contains_cjk("上游模型") is True
    assert _contains_cjk(_SENTINEL) is True
    assert _contains_cjk("⚠️") is False
    assert _contains_cjk("⚠️ Warning: low battery") is False
    assert _contains_cjk("") is False
