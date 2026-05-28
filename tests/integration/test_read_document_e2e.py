"""End-to-end: write a real document, call read_file, expect parsed text.

Uses the in-process workspace (no S3, no sandbox) so the test stays
hermetic but still exercises the full ``_read_file_handler`` →
``_handle_document`` → ``_parse_document_to_text`` → cache path.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from surogates.tools.builtin.file_ops import _read_file_handler
from tests.tools.fixtures.build_documents import (
    build_minimal_docx,
    build_minimal_pdf,
    build_minimal_pptx,
    build_minimal_xlsx,
)


@pytest.fixture
def isolated_document_cache(tmp_path, monkeypatch):
    """Swap the process-wide document cache for a fresh per-test cache."""
    from surogates.tools.utils import document_cache as cache_module

    fresh = cache_module.DocumentCache(
        root=tmp_path / "doc-cache",
        max_entries=8,
        max_entry_bytes=2 * 1024 * 1024,
    )
    monkeypatch.setattr(cache_module, "_DEFAULT", fresh)
    return fresh


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("builder", "filename", "probe"),
    [
        (build_minimal_pdf, "doc.pdf", "Hello PDF"),
        (build_minimal_docx, "doc.docx", "Hello DOCX"),
        (build_minimal_xlsx, "doc.xlsx", "Alpha"),
        (build_minimal_pptx, "doc.pptx", "Hello PPTX"),
    ],
)
async def test_read_file_returns_markdown_for_each_format(
    tmp_path: pathlib.Path,
    isolated_document_cache,
    builder,
    filename: str,
    probe: str,
) -> None:
    """Each office format must round-trip via read_file with no extra setup."""
    src = builder(tmp_path / filename)
    result_json = await _read_file_handler(
        {"path": str(src)},
        workspace_path=str(tmp_path),
    )
    result = json.loads(result_json)
    assert "error" not in result, result
    assert probe in result["content"], (
        f"Expected {probe!r} in markdown output for {filename}, "
        f"got: {result['content'][:200]}"
    )
    # All formats must respect the read_file envelope shape.
    assert result["path"] == str(src)
    assert result["total_lines"] >= 1
    assert result["lines_shown"] >= 1
    assert "offset" in result
    assert "limit" in result
    assert "truncated" in result


@pytest.mark.asyncio
async def test_read_file_pagination_round_trip(
    tmp_path: pathlib.Path,
    isolated_document_cache,
) -> None:
    """Paginated reads of the same document must produce disjoint windows
    and never re-parse — the cache makes pagination free.
    """
    from surogates.tools.builtin import file_ops

    src = build_minimal_docx(tmp_path / "windowed.docx")

    parse_calls = {"n": 0}
    original_parse = file_ops._parse_document_to_text

    async def counting_parse(path):
        parse_calls["n"] += 1
        return await original_parse(path)

    file_ops._parse_document_to_text = counting_parse
    try:
        first = json.loads(
            await _read_file_handler({"path": str(src), "offset": 1, "limit": 1})
        )
        second = json.loads(
            await _read_file_handler({"path": str(src), "offset": 2, "limit": 1})
        )
    finally:
        file_ops._parse_document_to_text = original_parse

    assert "error" not in first, first
    assert "error" not in second, second
    # Two different windows of the same file → exactly one parse.
    assert parse_calls["n"] == 1
    assert first["content"] != second["content"] or first["total_lines"] == 1
