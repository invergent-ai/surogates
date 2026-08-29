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
