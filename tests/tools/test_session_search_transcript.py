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
    ])

    assert "{" not in transcript


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
