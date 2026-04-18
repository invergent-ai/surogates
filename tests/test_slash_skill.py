"""Tests for eager slash-command skill expansion in the harness.

Covers ``surogates.harness.slash_skill`` -- parsing, message building, and
the end-to-end ``expand_slash_skill`` dispatch path with a mocked tool
registry.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from surogates.harness.slash_skill import (
    build_expanded_message,
    expand_slash_skill,
    parse_slash_command,
)


# ---------------------------------------------------------------------------
# parse_slash_command
# ---------------------------------------------------------------------------


class TestParseSlashCommand:
    def test_returns_name_and_args(self) -> None:
        assert parse_slash_command("/arxiv cuda training llm 2026") == (
            "arxiv",
            "cuda training llm 2026",
        )

    def test_returns_name_with_empty_args(self) -> None:
        assert parse_slash_command("/arxiv") == ("arxiv", "")

    def test_strips_outer_whitespace(self) -> None:
        assert parse_slash_command("   /arxiv  cuda  ") == ("arxiv", "cuda")

    def test_multiline_args_preserved(self) -> None:
        text = "/arxiv search query\nwith multiple lines"
        result = parse_slash_command(text)
        assert result == ("arxiv", "search query\nwith multiple lines")

    def test_hyphenated_name(self) -> None:
        assert parse_slash_command("/ocr-and-documents file.pdf") == (
            "ocr-and-documents",
            "file.pdf",
        )

    def test_underscored_name(self) -> None:
        assert parse_slash_command("/my_skill go") == ("my_skill", "go")

    def test_returns_none_for_clear(self) -> None:
        assert parse_slash_command("/clear") is None

    def test_returns_none_for_compress(self) -> None:
        assert parse_slash_command("/compress") is None

    def test_returns_none_when_no_leading_slash(self) -> None:
        assert parse_slash_command("arxiv cuda") is None

    def test_returns_none_for_empty_name(self) -> None:
        assert parse_slash_command("/") is None

    def test_returns_none_for_digit_starting_name(self) -> None:
        # Must start with a letter -- guards against ``/123`` etc.
        assert parse_slash_command("/123foo") is None

    def test_capitalised_name_preserved(self) -> None:
        # Names are passed through verbatim; downstream skill_view does the
        # case-sensitive lookup and will report "not found" for a typo.
        assert parse_slash_command("/Arxiv x") == ("Arxiv", "x")

    def test_trailing_whitespace_yields_empty_args(self) -> None:
        assert parse_slash_command("/arxiv   ") == ("arxiv", "")


# ---------------------------------------------------------------------------
# build_expanded_message
# ---------------------------------------------------------------------------


class TestBuildExpandedMessage:
    def test_includes_skill_name_and_args(self) -> None:
        msg = build_expanded_message(
            name="arxiv",
            args="cuda training",
            skill_body="# arXiv\n\nDo stuff.",
        )
        assert "`arxiv` skill" in msg
        assert "with: cuda training" in msg
        assert "# arXiv" in msg
        assert "User request: cuda training" in msg

    def test_omits_args_clause_when_empty(self) -> None:
        msg = build_expanded_message(name="arxiv", args="", skill_body="body")
        assert "with:" not in msg
        assert "User request:" not in msg

    def test_passes_through_staging_preamble_in_body(self) -> None:
        # The API route prepends the preamble to ``content``; this helper
        # must surface it verbatim rather than re-adding its own.
        body_with_preamble = (
            "> This skill is staged at `/workspace/.skills/arxiv`. "
            "All relative paths in this document resolve against that directory.\n\n"
            "# arXiv\nbody."
        )
        msg = build_expanded_message(
            name="arxiv", args="x", skill_body=body_with_preamble,
        )
        assert "/workspace/.skills/arxiv" in msg
        # No duplicated staging guidance from this helper itself.
        assert msg.count("staged at") == 1


# ---------------------------------------------------------------------------
# expand_slash_skill
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Captures dispatch calls and returns a canned ``skill_view`` payload."""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def dispatch(
        self, name: str, arguments: Any, **kwargs: Any,
    ) -> str:
        self.calls.append((name, arguments, kwargs))
        if self._payload is None:
            raise RuntimeError("skill_view unavailable")
        return json.dumps(self._payload)


class _FakeRegistryReturningInvalidJSON:
    async def dispatch(self, name: str, arguments: Any, **kwargs: Any) -> str:
        return "this is not json {{{"


@pytest.mark.asyncio
class TestExpandSlashSkill:
    async def test_expands_known_skill(self) -> None:
        registry = _FakeRegistry({
            "success": True,
            "name": "arxiv",
            "content": "# arxiv\n\nSearch papers.",
            "staged_at": "/workspace/.skills/arxiv",
        })
        result = await expand_slash_skill(
            text="/arxiv cuda training",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )

        assert result is not None
        expanded, skill_name, staged_at = result
        assert skill_name == "arxiv"
        assert staged_at == "/workspace/.skills/arxiv"
        assert "# arxiv" in expanded
        assert "cuda training" in expanded

        # Verify dispatch was called with the right tool + args.
        assert len(registry.calls) == 1
        call_name, call_args, call_kwargs = registry.calls[0]
        assert call_name == "skill_view"
        assert call_args == {"name": "arxiv"}
        assert call_kwargs["session_id"] == "sess-1"

    async def test_returns_none_for_non_slash_text(self) -> None:
        registry = _FakeRegistry({"success": True, "content": "x"})
        result = await expand_slash_skill(
            text="just a regular message",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None
        assert registry.calls == []  # never dispatched

    async def test_returns_none_for_builtin_clear(self) -> None:
        registry = _FakeRegistry({"success": True, "content": "x"})
        result = await expand_slash_skill(
            text="/clear",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None
        assert registry.calls == []

    async def test_returns_none_for_unknown_skill(self) -> None:
        registry = _FakeRegistry({
            "success": False,
            "error": "Skill 'nope' not found.",
        })
        result = await expand_slash_skill(
            text="/nope do thing",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None

    async def test_returns_none_when_dispatch_raises(self) -> None:
        registry = _FakeRegistry(payload=None)  # raises in dispatch
        result = await expand_slash_skill(
            text="/arxiv x",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None

    async def test_returns_none_when_dispatch_returns_non_json(self) -> None:
        result = await expand_slash_skill(
            text="/arxiv x",
            tools=_FakeRegistryReturningInvalidJSON(),
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None

    async def test_returns_none_when_skill_body_empty(self) -> None:
        registry = _FakeRegistry({
            "success": True,
            "name": "arxiv",
            "content": "",
        })
        result = await expand_slash_skill(
            text="/arxiv x",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None

    async def test_passes_api_client_and_session_factory_through(self) -> None:
        registry = _FakeRegistry({
            "success": True,
            "name": "arxiv",
            "content": "body",
            "staged_at": None,
        })
        sentinel_api = object()
        sentinel_factory = object()
        await expand_slash_skill(
            text="/arxiv x",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=sentinel_api,
            session_factory=sentinel_factory,
        )
        _, _, call_kwargs = registry.calls[0]
        assert call_kwargs["api_client"] is sentinel_api
        assert call_kwargs["session_factory"] is sentinel_factory
