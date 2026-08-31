"""kb_read_page pages through a long page instead of being spilled.

The tool used to cap itself at 200_000 bytes while layer 2 persisted
anything over 100_000 characters, so every page in that band was
guaranteed to be spilled to a file the model then could not open.  It
now returns a window and reports the offset for the next one, and is
pinned so the budget layers never persist it.
"""

from surogates.tools.builtin.kb_tools import (
    _PAGE_LIMIT_DEFAULT,
    _PAGE_LIMIT_MAX,
    _clamp_offset,
    _clamp_page_limit,
    _format_page_window,
)
from surogates.tools.utils.budget_config import DEFAULT_BUDGET


def test_short_page_is_returned_whole_with_no_note():
    out = _format_page_window("Title", "hello", 0, _PAGE_LIMIT_DEFAULT)
    assert out == "# Title\n\nhello"


def test_long_page_reports_the_next_offset():
    out = _format_page_window("Title", "x" * 1000, 0, 400)
    assert "Showing characters 0-400 of 1000" in out
    assert "Continue with offset=400" in out
    assert out.endswith("x" * 400)


def test_middle_and_final_windows_join_up():
    content = "".join(str(i % 10) for i in range(1000))
    first = _format_page_window("T", content, 0, 400)
    second = _format_page_window("T", content, 400, 400)
    final = _format_page_window("T", content, 800, 400)

    assert "Continue with offset=400" in first
    assert "Continue with offset=800" in second
    assert "End of page" in final
    assert "Continue with offset" not in final

    # The windows must reconstruct the page exactly -- no gaps, no overlap.
    rebuilt = "".join(
        part.split("_\n\n", 1)[1] for part in (first, second, final)
    )
    assert rebuilt == content


def test_offset_past_the_end_is_an_error_not_an_empty_page():
    out = _format_page_window("T", "abc", 99, 10)
    assert "past the end" in out


def test_empty_page_is_not_an_error():
    assert _format_page_window("T", "", 0, 10) == "# T\n\n"


def test_limit_is_clamped_and_offset_coerced():
    assert _clamp_page_limit(None) == _PAGE_LIMIT_DEFAULT
    assert _clamp_page_limit("nonsense") == _PAGE_LIMIT_DEFAULT
    assert _clamp_page_limit(0) == _PAGE_LIMIT_DEFAULT
    assert _clamp_page_limit(-5) == _PAGE_LIMIT_DEFAULT
    assert _clamp_page_limit(10**9) == _PAGE_LIMIT_MAX
    assert _clamp_page_limit(1234) == 1234

    assert _clamp_offset(None) == 0
    assert _clamp_offset(-3) == 0
    assert _clamp_offset("12") == 12


def test_a_full_window_stays_under_the_spill_threshold():
    """The whole point: a page read can never trip layer 2."""
    threshold = DEFAULT_BUDGET.resolve_threshold("some_other_tool")
    biggest = len(_format_page_window("T", "x" * 10**6, 0, _PAGE_LIMIT_MAX))
    assert biggest < threshold


def test_kb_read_page_is_pinned_against_persistence():
    assert DEFAULT_BUDGET.resolve_threshold("kb_read_page") == float("inf")


# --- Page ranges into the original document -------------------------------
#
# A summary page names each section with the pages it covers
# ("# Riscuri acoperite (pages 7-11)"), but the source text behind it carries
# no embedding, so search can never surface it. Without a way to ask for a
# span, an agent could see that a section existed and still not read its
# actual wording -- only somebody's summary of it.

import json as _json

from surogates.tools.builtin.kb_tools import (
    _format_page_range,
    _parse_page_range,
)


def _pages(n: int = 14) -> bytes:
    return _json.dumps(
        [{"page": i, "content": f"body {i}"} for i in range(1, n + 1)]
    ).encode()


def test_parses_the_range_a_summary_page_prints():
    # The renderer emits an en dash, so an agent copying what it just read
    # must not be told its input is malformed.
    assert _parse_page_range("7-11") == (7, 11)
    assert _parse_page_range("7–11") == (7, 11)
    assert _parse_page_range("7") == (7, 7)
    assert _parse_page_range("  9 - 12  ") == (9, 12)


def test_rejects_ranges_that_cannot_mean_anything():
    assert _parse_page_range("junk") is None
    assert _parse_page_range("11-7") is None
    assert _parse_page_range("0-3") is None
    assert _parse_page_range(None) is None


def test_caps_the_span_so_one_call_cannot_pull_a_whole_book():
    start, end = _parse_page_range("1-500")
    assert (start, end) == (1, 40)


def test_returns_exactly_the_requested_pages():
    out = _format_page_range("Doc", _pages(), 7, 9)
    assert "body 7" in out and "body 8" in out and "body 9" in out
    assert "body 6" not in out and "body 10" not in out
    assert "## Page 7" in out


def test_selects_by_the_documents_own_page_numbers():
    # Pages are chosen by the stored page number, not list position, so a
    # gap in extraction cannot silently shift every later reference.
    raw = _json.dumps([
        {"page": 1, "content": "first"},
        {"page": 5, "content": "fifth"},
        {"page": 6, "content": "sixth"},
    ]).encode()
    out = _format_page_range("Doc", raw, 5, 5)
    assert "fifth" in out and "first" not in out and "sixth" not in out


def test_a_range_past_the_end_says_what_is_available():
    out = _format_page_range("Doc", _pages(), 90, 95)
    assert "1-14" in out, out


def test_a_markdown_page_is_not_treated_as_a_page_array():
    out = _format_page_range("Doc", b"# just markdown", 1, 2)
    assert out.startswith("Error:")
    assert "Omit `pages`" in out
