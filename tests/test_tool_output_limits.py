"""Tests for configurable tool output limits."""

from __future__ import annotations

from surogates.tools.utils.tool_output_limits import get_tool_output_limits


def test_tool_output_limits_use_defaults(monkeypatch) -> None:
    monkeypatch.delenv("SUROGATES_TOOL_OUTPUT_MAX_BYTES", raising=False)
    monkeypatch.delenv("SUROGATES_TOOL_OUTPUT_MAX_LINES", raising=False)
    monkeypatch.delenv("SUROGATES_TOOL_OUTPUT_MAX_LINE_LENGTH", raising=False)

    limits = get_tool_output_limits()

    assert limits.max_bytes == 50_000
    assert limits.max_lines == 2000
    assert limits.max_line_length == 2000


def test_tool_output_limits_read_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("SUROGATES_TOOL_OUTPUT_MAX_BYTES", "1234")
    monkeypatch.setenv("SUROGATES_TOOL_OUTPUT_MAX_LINES", "321")
    monkeypatch.setenv("SUROGATES_TOOL_OUTPUT_MAX_LINE_LENGTH", "99")

    limits = get_tool_output_limits()

    assert limits.max_bytes == 1234
    assert limits.max_lines == 321
    assert limits.max_line_length == 99


def test_terminal_truncation_uses_configured_byte_limit(monkeypatch) -> None:
    from surogates.tools.builtin import terminal

    monkeypatch.setenv("SUROGATES_TOOL_OUTPUT_MAX_BYTES", "100")

    result = terminal._truncate_output("x" * 150)

    assert len(result) > 100
    assert "OUTPUT TRUNCATED" in result
    assert "50 chars omitted" in result
