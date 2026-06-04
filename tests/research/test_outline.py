"""Tests for living-outline pure logic.

The outline is a markdown document the planner rewrites as the
research direction evolves.  These helpers keep it tidy and let
callers enumerate its sections (used by the writer to drive
section-by-section synthesis).
"""

from __future__ import annotations

from surogates.research.outline import normalize_outline, outline_sections


def test_normalize_strips_trailing_spaces_per_line() -> None:
    raw = "alpha   \nbeta\t\n"
    out = normalize_outline(raw)
    # No line ends with trailing whitespace.
    assert all(not line.endswith((" ", "\t")) for line in out.splitlines())


def test_normalize_collapses_blank_line_runs() -> None:
    raw = "# Title\n\n\n\n## Section\n\n\n\nbody\n\n\n"
    out = normalize_outline(raw)
    # At most one blank line in a row.
    assert "\n\n\n" not in out


def test_normalize_strips_leading_and_trailing_blank_lines() -> None:
    """A planner that prepended an empty draft line should not see
    the outline grow unbounded across re-saves."""

    raw = "\n\n# Title\n\nbody\n\n\n"
    out = normalize_outline(raw)
    assert out.startswith("# Title")
    assert out.endswith("body")


def test_normalize_preserves_content_order() -> None:
    raw = "# T\n## A\nl1\nl2\n## B\nl3\n"
    out = normalize_outline(raw)
    assert out.index("## A") < out.index("## B")
    assert "l1\nl2" in out
    assert out.endswith("l3")


def test_outline_sections_extracts_markdown_headings_level_2_plus() -> None:
    raw = "# Report\n## Background\ntext\n## Methods\nmore\n### Sub\n"
    # The top-level ``# Report`` heading is excluded — the writer
    # synthesizes one section per ``##`` (and below) heading.
    assert outline_sections(raw) == ["Background", "Methods", "Sub"]


def test_outline_sections_empty_when_no_headings() -> None:
    assert outline_sections("just prose, no headings") == []


def test_outline_sections_strips_trailing_whitespace_from_heading() -> None:
    """Trailing whitespace in a heading title is a common copy-paste
    artifact and must not change the section identifier."""

    assert outline_sections("## Background   \nbody") == ["Background"]


def test_outline_sections_ignores_atx_in_code_blocks_is_not_required() -> None:
    """Document the chosen behavior: we do NOT parse fenced code
    blocks.  An accidental ``## inside fence`` will be counted as a
    section.  That matches how the planner is told to use the
    outline (markdown headings only at the top level)."""

    raw = "## A\n```\n## not a section\n```\n"
    # Both ``##`` lines are extracted because the loader is heading-only
    # and does not maintain a code-block state machine.
    assert outline_sections(raw) == ["A", "not a section"]
