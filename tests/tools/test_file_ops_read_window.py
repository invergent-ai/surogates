"""Read-window rendering: overflow must yield content plus a resume offset.

An over-budget read used to return ``{"error": ...}`` with no content, which
costs a full round trip and tells the model nothing about where to continue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surogates.tools.builtin.file_ops import (
    _read_file_handler,
    _render_read_window,
)


class TestRenderReadWindow:
    def test_whole_file_has_no_next_offset(self) -> None:
        lines = ["a\n", "b\n", "c\n"]
        content, shown, next_offset = _render_read_window(lines, 1, 3)
        assert content == "a\nb\nc\n"
        assert shown == 3
        assert next_offset is None

    def test_partial_window_reports_resume_point(self) -> None:
        lines = ["b\n", "c\n"]
        content, shown, next_offset = _render_read_window(lines, 2, 5)
        assert content == "b\nc\n"
        assert shown == 2
        assert next_offset == 4

    def test_over_budget_cuts_at_line_boundary(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "surogates.tools.builtin.file_ops.get_max_bytes", lambda: 10,
        )
        lines = ["aaaa\n", "bbbb\n", "cccc\n"]  # 5 chars each
        content, shown, next_offset = _render_read_window(lines, 1, 3)
        assert content == "aaaa\nbbbb\n"
        assert shown == 2
        assert next_offset == 3

    def test_single_oversized_line_still_returns_content(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "surogates.tools.builtin.file_ops.get_max_bytes", lambda: 10,
        )
        content, shown, next_offset = _render_read_window(["x" * 100 + "\n"], 1, 1)
        assert content == "x" * 10
        assert shown == 1
        assert next_offset is None


@pytest.mark.asyncio
async def test_oversized_read_returns_content_not_error(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        "surogates.tools.builtin.file_ops.get_max_bytes", lambda: 40,
    )
    src = tmp_path / "big.txt"
    src.write_text("".join(f"line {i}\n" for i in range(1, 51)), encoding="utf-8")

    result = json.loads(await _read_file_handler({"path": str(src)}))

    assert "error" not in result, result
    assert result["content"].startswith("line 1\n")
    assert result["truncated"] is True
    assert result["next_offset"] == result["lines_shown"] + 1
    assert "offset=" in result["_hint"]
