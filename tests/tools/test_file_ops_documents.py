"""Read documents through the file tool, including pagination, caching and errors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from surogates.tools.builtin.file_ops import _read_file_handler
from tests.tools.fixtures.build_documents import (
    build_minimal_docx,
    build_minimal_pdf,
    build_minimal_pptx,
    build_minimal_xlsx,
)


def _fake_liteparse_raising(exc: Exception):
    """Build a fake ``LiteParse`` class whose ``.parse()`` raises ``exc``."""

    class FakeLiteParse:
        def __init__(self, **kwargs):
            pass

        def parse(self, path_str):
            raise exc

    return FakeLiteParse


# ---------------------------------------------------------------------------
# _handle_document happy path + error envelope + cache integration
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_document_cache(tmp_path, monkeypatch):
    """Swap the process-wide document cache for a fresh per-test cache.

    Ensures tests don't leak state via /tmp/surogates-read-cache.
    """
    from surogates.tools.utils import document_cache as cache_module

    fresh = cache_module.DocumentCache(
        root=tmp_path / "doc-cache",
        max_entries=8,
        max_entry_bytes=2 * 1024 * 1024,
    )
    monkeypatch.setattr(cache_module, "_DEFAULT", fresh)
    return fresh


@pytest.mark.asyncio
async def test_read_pdf_returns_text_via_handler(
    tmp_path: Path, isolated_document_cache,
) -> None:
    pdf = build_minimal_pdf(tmp_path / "p.pdf", heading="Hello PDF")
    result_json = await _read_file_handler({"path": str(pdf)})
    result = json.loads(result_json)
    assert "error" not in result, result
    assert "Hello PDF" in result["content"]
    assert result["total_lines"] > 0
    assert result["truncated"] is False
    assert result["path"] == str(pdf)


@pytest.mark.asyncio
async def test_read_docx_returns_text_via_handler(
    tmp_path: Path, isolated_document_cache,
) -> None:
    docx = build_minimal_docx(tmp_path / "d.docx")
    result_json = await _read_file_handler({"path": str(docx)})
    result = json.loads(result_json)
    assert "error" not in result, result
    assert "Hello DOCX" in result["content"]


@pytest.mark.asyncio
async def test_read_xlsx_includes_both_sheet_names_via_handler(
    tmp_path: Path, isolated_document_cache,
) -> None:
    xlsx = build_minimal_xlsx(tmp_path / "x.xlsx")
    result_json = await _read_file_handler({"path": str(xlsx)})
    result = json.loads(result_json)
    assert "error" not in result, result
    assert "Alpha" in result["content"]
    assert "Beta" in result["content"]


@pytest.mark.asyncio
async def test_read_pptx_includes_slide_text_via_handler(
    tmp_path: Path, isolated_document_cache,
) -> None:
    pptx = build_minimal_pptx(tmp_path / "p.pptx")
    result_json = await _read_file_handler({"path": str(pptx)})
    result = json.loads(result_json)
    assert "error" not in result, result
    assert "Hello PPTX" in result["content"]


@pytest.mark.asyncio
async def test_pagination_via_offset_limit(
    tmp_path: Path, isolated_document_cache, monkeypatch,
) -> None:
    """offset/limit slice the rendered text by 1-indexed lines."""
    from surogates.tools.builtin import file_ops

    fake_text = "\n".join(f"line {i}" for i in range(1, 101)) + "\n"

    async def fake_parse(path: Path) -> str:
        return fake_text

    monkeypatch.setattr(file_ops, "_parse_document_to_text", fake_parse)

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF placeholder")

    result_json = await _read_file_handler(
        {"path": str(pdf), "offset": 50, "limit": 5},
    )
    result = json.loads(result_json)
    assert "error" not in result, result
    # Content is emitted verbatim, in the same format as _handle_text.
    assert result["content"] == "".join(f"line {i}\n" for i in range(50, 55))
    assert result["truncated"] is True
    assert result["next_offset"] == 55
    assert result["offset"] == 50
    assert result["limit"] == 5


@pytest.mark.asyncio
async def test_corrupt_document_returns_fallback_hint(
    tmp_path: Path, isolated_document_cache, monkeypatch,
) -> None:
    from surogates.tools.builtin import file_ops

    fake = _fake_liteparse_raising(RuntimeError("not a pdf"))
    monkeypatch.setattr(file_ops, "_load_liteparse", lambda: fake)

    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"%PDF-1.4 placeholder")
    result_json = await _read_file_handler({"path": str(bad)})
    result = json.loads(result_json)
    assert "error" in result
    err = result["error"].lower()
    assert "pdftotext" in err or "pandoc" in err
    assert "corrupt.pdf" in result["error"]


@pytest.mark.asyncio
async def test_document_cache_hit_skips_reparse(
    tmp_path: Path, isolated_document_cache, monkeypatch,
) -> None:
    from surogates.tools.builtin import file_ops

    calls = {"n": 0}

    async def counting_parse(path: Path) -> str:
        calls["n"] += 1
        return "# header\n" + "\n".join(f"line {i}" for i in range(50)) + "\n"

    monkeypatch.setattr(file_ops, "_parse_document_to_text", counting_parse)

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF placeholder")

    # First read populates the cache.
    await _read_file_handler({"path": str(pdf)})
    # Different window — must hit the cache, not re-parse.
    await _read_file_handler({"path": str(pdf), "offset": 10, "limit": 5})
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_missing_document_returns_clean_error(
    tmp_path: Path, isolated_document_cache,
) -> None:
    missing = tmp_path / "ghost.pdf"
    result_json = await _read_file_handler({"path": str(missing)})
    result = json.loads(result_json)
    assert "error" in result
    assert "not found" in result["error"].lower()
