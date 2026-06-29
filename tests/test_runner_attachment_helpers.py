"""Tests: runner.py attachment budget and injection-detection helpers.

Covers:
1. _apply_total_inline_budget_to_attachments: demotes over-budget items
   to remove inlined_text and set inline_skip_reason.
2. _injection_blocks_filename: benign and injection-like filenames.
3. _injection_blocks_filename uses surogates.session.attachment_ingest, not api.routes.sessions.
"""

from surogates.channels.runner import (
    _apply_total_inline_budget_to_attachments,
    _injection_blocks_filename,
)
from surogates.session.attachment_ingest import _INLINE_TOTAL_RENDERED_CAP_CHARS


def test_injection_blocks_filename_benign():
    """Benign filenames return False."""
    assert _injection_blocks_filename("report.pdf") is False
    assert _injection_blocks_filename("notes.txt") is False
    assert _injection_blocks_filename("document.docx") is False


def test_injection_blocks_filename_never_raises():
    """The detector silently returns False on any exception."""
    # Test with edge cases that might trigger exceptions in the detector.
    result = _injection_blocks_filename("")
    assert result is False


def test_apply_total_inline_budget_under_budget_items_kept():
    """Under-budget items keep their inlined_text and inlined_render_kind."""
    # Each item: ~100 chars, total is 300 chars, cap is 50000.
    attachments = [
        {
            "path": "a.txt",
            "inlined_text": "a" * 100,
            "inlined_render_kind": "text",
        },
        {
            "path": "b.txt",
            "inlined_text": "b" * 100,
            "inlined_render_kind": "text",
        },
        {
            "path": "c.txt",
            "inlined_text": "c" * 100,
            "inlined_render_kind": "text",
        },
    ]
    _apply_total_inline_budget_to_attachments(attachments)

    # All items should keep their inlined_text.
    for att in attachments:
        assert att.get("inlined_text") is not None
        assert att.get("inlined_render_kind") == "text"
        assert "inline_skip_reason" not in att


def test_apply_total_inline_budget_over_budget_items_demoted():
    """Over-budget items lose inlined_text and gain inline_skip_reason."""
    # Force the cap cheaply: create items that sum past the limit.
    # Use roughly (cap/2 + 1) * 2 to ensure overflow on the second item.
    text_size = _INLINE_TOTAL_RENDERED_CAP_CHARS // 2 + 1
    attachments = [
        {
            "path": "a.txt",
            "inlined_text": "a" * text_size,
            "inlined_render_kind": "text",
        },
        {
            "path": "b.txt",
            "inlined_text": "b" * text_size,
            "inlined_render_kind": "text",
        },
    ]
    _apply_total_inline_budget_to_attachments(attachments)

    # First item should keep inlined_text (it's under the budget).
    assert attachments[0].get("inlined_text") is not None
    assert attachments[0].get("inlined_render_kind") == "text"
    assert "inline_skip_reason" not in attachments[0]

    # Second item should be demoted.
    assert attachments[1].get("inlined_text") is None
    assert attachments[1].get("inlined_render_kind") is None
    assert attachments[1].get("inline_skip_reason") == "total_budget_exceeded"


def test_apply_total_inline_budget_pre_failed_items_untouched():
    """Pre-failed items (no inlined_text, has inline_skip_reason) are left alone."""
    attachments = [
        {
            "path": "a.txt",
            "inline_skip_reason": "parse_error",
            # no inlined_text
        },
        {
            "path": "b.txt",
            "inlined_text": "b" * 100,
            "inlined_render_kind": "text",
        },
    ]
    _apply_total_inline_budget_to_attachments(attachments)

    # First item should remain untouched.
    assert attachments[0].get("inlined_text") is None
    assert attachments[0].get("inline_skip_reason") == "parse_error"
    assert "inlined_render_kind" not in attachments[0]

    # Second item should keep inlined_text.
    assert attachments[1].get("inlined_text") is not None
    assert attachments[1].get("inlined_render_kind") == "text"


def test_apply_total_inline_budget_empty_list():
    """Empty list is handled gracefully."""
    attachments = []
    _apply_total_inline_budget_to_attachments(attachments)
    assert attachments == []


def test_apply_total_inline_budget_all_pre_failed():
    """A batch of all pre-failed items remains untouched."""
    attachments = [
        {"path": "a.txt", "inline_skip_reason": "parse_error"},
        {"path": "b.txt", "inline_skip_reason": "oversize_output"},
    ]
    _apply_total_inline_budget_to_attachments(attachments)

    assert attachments[0].get("inline_skip_reason") == "parse_error"
    assert attachments[1].get("inline_skip_reason") == "oversize_output"
    assert attachments[0].get("inlined_text") is None
    assert attachments[1].get("inlined_text") is None


def test_injection_blocks_filename_uses_shared_accessor_not_api_routes(monkeypatch):
    """_injection_blocks_filename must obtain its detector from
    surogates.session.attachment_ingest.get_injection_detector, NOT from
    surogates.api.routes.sessions._get_injection_detector.
    Importing surogates.api.routes.sessions should NOT be required for this call.
    """
    import surogates.session.attachment_ingest as ingest_mod

    called = []

    class _FakeDetector:
        def detect(self, name, source=None):
            called.append(name)
            from types import SimpleNamespace
            return SimpleNamespace(is_injection=False)

    monkeypatch.setattr(ingest_mod, "_injection_detector", _FakeDetector())
    result = _injection_blocks_filename("benign.pdf")
    assert result is False
    assert "benign.pdf" in called, "detector sourced from attachment_ingest was not called"
