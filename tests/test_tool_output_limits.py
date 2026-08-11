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


def test_terminal_truncation_keeps_the_tail_and_spills_the_rest(
    monkeypatch, tmp_path,
) -> None:
    """The end of a command's output is where it says what happened."""
    from surogates.tools.builtin import terminal

    monkeypatch.setenv("SUROGATES_TOOL_OUTPUT_MAX_BYTES", "100")
    monkeypatch.setattr(terminal.tempfile, "tempdir", str(tmp_path))

    output = "START" + ("x" * 500) + "ASSERTION FAILED"
    result = terminal._truncate_output(output)

    assert result.startswith("START")
    assert result.endswith("ASSERTION FAILED")
    # Tail gets the larger share of the budget.
    head, _, tail = result.partition("...\n\n")
    assert len(tail) > 100 * 0.5

    spilled = [p for p in tmp_path.iterdir() if p.name.startswith("terminal-output-")]
    assert len(spilled) == 1
    assert spilled[0].read_text() == output
    assert str(spilled[0]) in result


def test_terminal_truncation_survives_a_failed_spill(monkeypatch) -> None:
    from surogates.tools.builtin import terminal

    monkeypatch.setenv("SUROGATES_TOOL_OUTPUT_MAX_BYTES", "100")

    def boom(*a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(terminal.tempfile, "mkstemp", boom)

    result = terminal._truncate_output("x" * 500)
    assert "OUTPUT TRUNCATED" in result
    assert "Re-run with a narrower command" in result
