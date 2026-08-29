"""The last-resort conclusion must extract, never invent."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.harness.loop_conclusion import (
    _render_transcript,
    conclude_from_transcript,
)


def _client(content: str) -> AsyncMock:
    c = AsyncMock()
    c.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    return c


MESSAGES = [
    {"role": "system", "content": "ignored"},
    {"role": "user", "content": "How many albums?"},
    {"role": "assistant", "content": "Let me check the discography...."},
]


class TestNeverInvents:
    """A model asked for an answer will produce one whether or not the
    evidence exists -- that is how a missing transcription became a
    fabricated shopping list. NOT_FOUND must survive to the caller as None."""

    async def test_not_found_returns_none(self) -> None:
        got = await conclude_from_transcript(
            _client("NOT_FOUND"), "m", question="q", messages=MESSAGES,
        )
        assert got is None

    @pytest.mark.parametrize("reply", ["NOT_FOUND.", ' "NOT_FOUND" ', "not_found"])
    async def test_decline_is_recognised_however_wrapped(self, reply: str) -> None:
        got = await conclude_from_transcript(
            _client(reply), "m", question="q", messages=MESSAGES,
        )
        assert got is None

    async def test_answer_mentioning_the_token_is_kept(self) -> None:
        """Only a bare decline counts -- a real conclusion that happens to
        discuss NOT_FOUND is still a conclusion."""
        got = await conclude_from_transcript(
            _client("The lookup returned NOT_FOUND for two of five rows; the count is 3"),
            "m", question="q", messages=MESSAGES,
        )
        assert got is not None and got.endswith("3")


class TestFailsQuietly:
    """Every uncertain path must leave the caller's existing ending intact."""

    async def test_no_client_configured(self) -> None:
        assert await conclude_from_transcript(
            None, "m", question="q", messages=MESSAGES) is None

    async def test_no_model_configured(self) -> None:
        assert await conclude_from_transcript(
            _client("x"), "", question="q", messages=MESSAGES) is None

    async def test_call_failure_is_swallowed(self) -> None:
        c = AsyncMock()
        c.chat.completions.create.side_effect = RuntimeError("upstream down")
        assert await conclude_from_transcript(
            c, "m", question="q", messages=MESSAGES) is None

    async def test_empty_reply(self) -> None:
        assert await conclude_from_transcript(
            _client("   "), "m", question="q", messages=MESSAGES) is None

    async def test_empty_transcript(self) -> None:
        assert await conclude_from_transcript(
            _client("42"), "m", question="q",
            messages=[{"role": "system", "content": "sys"}]) is None


class TestTranscript:
    def test_system_dropped_and_roles_kept(self) -> None:
        out = _render_transcript(MESSAGES)
        assert "ignored" not in out
        assert "[user] How many albums?" in out

    def test_tool_only_turn_keeps_its_intent(self) -> None:
        out = _render_transcript([
            {"role": "assistant", "content": None, "tool_calls": [
                {"function": {"name": "web_search"}},
                {"function": {"name": "read_file"}},
            ]},
        ])
        assert "web_search" in out and "read_file" in out

    def test_truncates_from_the_front(self) -> None:
        """A conclusion rests on the most recent findings; the opening of a
        long session is setup."""
        msgs = [{"role": "assistant", "content": "x" * 40_000},
                {"role": "assistant", "content": "THE FINDING"}]
        out = _render_transcript(msgs)
        assert out.startswith("...")
        assert "THE FINDING" in out
