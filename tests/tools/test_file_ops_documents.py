"""Tests for document-aware read_file behavior.

Layered as:
  - Regression snapshot for the text path (proves the refactor is mechanical).
  - Document-format unit tests (PDF / docx / xlsx / pptx).
  - Cache behavior tests.
  - Error envelope tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surogates.tools.builtin.file_ops import _read_file_handler


@pytest.mark.asyncio
async def test_text_path_unchanged_after_refactor(tmp_path: Path) -> None:
    """Reading a .py file must return the same JSON shape and content as
    before the refactor.  Acts as a regression guard for Task 1.
    """
    src = tmp_path / "hello.py"
    src.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    result_json = await _read_file_handler({"path": str(src)})
    result = json.loads(result_json)
    assert result["content"] == "1|a = 1\n2|b = 2\n3|c = 3\n"
    assert result["total_lines"] == 3
    assert result["lines_shown"] == 3
    assert result["truncated"] is False
    assert result["offset"] == 1
