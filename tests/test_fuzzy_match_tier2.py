"""Regression tests for fuzzy match write safety."""

from __future__ import annotations

from surogates.tools.utils.fuzzy_match import fuzzy_find_and_replace


def test_escape_drift_rejects_backslash_quote_artifact() -> None:
    content = "message = \"don't insert escapes\"\n"
    old_string = "message = \\\"don't insert escapes\\\""
    new_string = "message = \\\"don't write backslash quotes\\\""

    new_content, count, error = fuzzy_find_and_replace(
        content, old_string, new_string,
    )

    assert new_content == content
    assert count == 0
    assert error is not None
    assert "Escape-drift detected" in error
