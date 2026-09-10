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
)


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
