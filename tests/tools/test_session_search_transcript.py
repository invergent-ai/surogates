# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
#
"""The transcript handed to the session-search summarizer.

Every case here is a payload shape the formatter used to read from the
wrong key, which is how a feature that looked like it worked produced
summaries built from user messages and bare tool names.
"""
from __future__ import annotations

from surogates.tools.builtin.session_search import _format_conversation


def test_assistant_text_is_read_from_the_nested_message() -> None:
    """``llm.response`` nests its text under ``message``.

    A flat ``data["content"]`` read returns nothing for every assistant
    turn, so the summarizer never saw a single agent reply.
    """
    transcript = _format_conversation([
        {"type": "llm.response", "data": {"message": {"content": "Atoms vibrate."}}},
    ])

    assert "[ASSISTANT]: Atoms vibrate." in transcript


def test_tool_output_is_read_from_content_not_result() -> None:
    """``result`` has never been written; every tool line rendered empty."""
    transcript = _format_conversation([
        {"type": "tool.result", "data": {"name": "lookup", "content": "42 g/mol"}},
    ])

    assert "[TOOL:lookup]: 42 g/mol" in transcript


def test_recaps_are_included() -> None:
    transcript = _format_conversation([
        {"type": "turn.summary", "data": {"recap": "Student grasped melting."}},
        {"type": "iteration.summary", "data": {"summary": "Checked the answer."}},
    ])

    assert "[RECAP]: Student grasped melting." in transcript
    assert "[RECAP]: Checked the answer." in transcript


def test_streamed_deltas_are_not_in_the_transcript() -> None:
    """Deltas repeat the assistant text one row per token chunk.

    They carry a ``content`` key, so without an explicit skip they fall
    through to the generic branch and re-emit the whole reply as hundreds
    of fragments — which then dominate the character budget and push the
    real conversation out of the truncation window.
    """
    transcript = _format_conversation([
        {"type": "llm.response", "data": {"message": {"content": "Atoms vibrate."}}},
        {"type": "llm.delta", "data": {"content": "Atoms "}},
        {"type": "llm.delta", "data": {"content": "vibrate."}},
    ])

    assert "LLM.DELTA" not in transcript
    assert transcript.count("Atoms vibrate.") == 1


def test_thinking_is_not_in_the_transcript() -> None:
    transcript = _format_conversation([
        {"type": "llm.thinking", "data": {"content": "let me reason"}},
    ])

    assert transcript == ""


def test_a_dict_message_is_never_rendered_as_a_repr() -> None:
    """The generic branch used to fall back to ``data["message"]``.

    On any LLM-shaped payload that is a dict, so the transcript grew a
    Python repr of the whole message object.
    """
    transcript = _format_conversation([
        {"type": "some.other", "data": {"message": {"content": "nested"}}},
        {"type": "some.other", "data": {"content": {"nested": "dict"}}},
        {"type": "some.other", "data": {"content": "plain text survives"}},
    ])

    assert "{" not in transcript
    # Positive control: the branch still renders genuine text, so the
    # assertion above is about repr suppression rather than the branch
    # having stopped emitting anything at all.
    assert "plain text survives" in transcript


def test_tool_calls_are_named_from_either_nesting() -> None:
    transcript = _format_conversation([
        {"type": "llm.response", "data": {
            "message": {
                "content": "",
                "tool_calls": [{"function": {"name": "search_web"}}],
            },
        }},
    ])

    assert "search_web" in transcript


# ── Centring the truncation window ─────────────────────────────────


def _long_transcript(needle: str) -> str:
    """Filler either side of the needle, far enough out to matter.

    Each half is comfortably wider than ``MAX_SESSION_CHARS``, so a window
    anchored at the start of the transcript cannot reach the needle — which
    is what makes these assertions fail when the centring is wrong rather
    than passing by luck.

    The filler says "work order" deliberately: it contains the substring
    "or", which is what a naive split on the query turns the OR operator
    into.
    """
    filler = "[USER]: we should coordinate on the work order.\n\n" * 6000
    return filler + f"[USER]: {needle}\n\n" + filler


def test_or_syntax_still_centres_on_the_match() -> None:
    """The tool tells the model to use OR; the window has to understand it.

    Splitting the query on whitespace makes "or" a search term, and "or"
    occurs inside "work order" in the first hundred characters of almost
    any transcript — pinning the window to the start and handing the
    summarizer text that does not contain what it was asked about.
    """
    from surogates.tools.builtin.session_search import _truncate_around_matches

    text = _long_transcript("we settled on baseten for inference")
    out = _truncate_around_matches(text, "elevenlabs OR baseten OR funding")

    assert "baseten" in out


def test_a_quoted_phrase_still_centres_on_the_match() -> None:
    from surogates.tools.builtin.session_search import _truncate_around_matches

    text = _long_transcript("the docker networking setup broke")
    out = _truncate_around_matches(text, '"docker networking"')

    assert "docker networking" in out


def test_operators_are_not_treated_as_search_terms() -> None:
    """The centring terms are the words, never the query syntax.

    ``OR`` is the operator, ``-java`` is an exclusion, and the quotes
    around a phrase are not part of it. Each would otherwise be looked up
    literally in the transcript — and "or" in particular matches inside
    ordinary words like "order", which is what pins the window to the
    start of the conversation.
    """
    from surogates.tools.builtin.session_search import _centering_terms

    assert _centering_terms("elevenlabs OR baseten") == ["elevenlabs", "baseten"]
    assert _centering_terms('"docker networking"') == ["docker", "networking"]
    assert _centering_terms("python -java") == ["python"]
    assert _centering_terms("-java") == []


def test_an_unbalanced_quote_does_not_raise() -> None:
    """tsquery tolerates it, so the window must too."""
    from surogates.tools.builtin.session_search import _truncate_around_matches

    text = _long_transcript("the reagent question")
    _truncate_around_matches(text, '"reagent')
