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
from tests.tools.fixtures.build_documents import (
    build_minimal_docx,
    build_minimal_pdf,
    build_minimal_pptx,
    build_minimal_xlsx,
)


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


@pytest.mark.asyncio
async def test_pdf_routed_to_document_handler(tmp_path: Path) -> None:
    """A .pdf path must reach _handle_document, not the binary-error path.

    Until Task 5 wires the handler, _handle_document raises
    NotImplementedError, which the outer try/except converts into a
    generic tool error.  We assert on the error envelope shape — not the
    wording — so the test stays green once Task 5 lands.
    """
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 placeholder")
    result_json = await _read_file_handler({"path": str(src)})
    result = json.loads(result_json)
    assert "Cannot read binary file" not in result.get("error", "")


@pytest.mark.asyncio
async def test_docx_no_longer_blocked_as_binary(tmp_path: Path) -> None:
    src = tmp_path / "doc.docx"
    src.write_bytes(b"PK\x03\x04 placeholder")
    result_json = await _read_file_handler({"path": str(src)})
    result = json.loads(result_json)
    assert "Cannot read binary file" not in result.get("error", "")


@pytest.mark.asyncio
async def test_xlsx_no_longer_blocked_as_binary(tmp_path: Path) -> None:
    src = tmp_path / "doc.xlsx"
    src.write_bytes(b"PK\x03\x04 placeholder")
    result_json = await _read_file_handler({"path": str(src)})
    result = json.loads(result_json)
    assert "Cannot read binary file" not in result.get("error", "")


@pytest.mark.asyncio
async def test_pptx_no_longer_blocked_as_binary(tmp_path: Path) -> None:
    src = tmp_path / "doc.pptx"
    src.write_bytes(b"PK\x03\x04 placeholder")
    result_json = await _read_file_handler({"path": str(src)})
    result = json.loads(result_json)
    assert "Cannot read binary file" not in result.get("error", "")


@pytest.mark.asyncio
async def test_legacy_doc_still_blocked(tmp_path: Path) -> None:
    src = tmp_path / "legacy.doc"
    src.write_bytes(b"placeholder")
    result_json = await _read_file_handler({"path": str(src)})
    result = json.loads(result_json)
    assert "Cannot read binary file" in result["error"]


@pytest.mark.asyncio
async def test_legacy_xls_still_blocked(tmp_path: Path) -> None:
    src = tmp_path / "legacy.xls"
    src.write_bytes(b"placeholder")
    result_json = await _read_file_handler({"path": str(src)})
    result = json.loads(result_json)
    assert "Cannot read binary file" in result["error"]


@pytest.mark.asyncio
async def test_legacy_ppt_still_blocked(tmp_path: Path) -> None:
    src = tmp_path / "legacy.ppt"
    src.write_bytes(b"placeholder")
    result_json = await _read_file_handler({"path": str(src)})
    result = json.loads(result_json)
    assert "Cannot read binary file" in result["error"]


# ---------------------------------------------------------------------------
# Task 3 — parser unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_pdf_returns_markdown(tmp_path: Path) -> None:
    """The parser must invoke markitdown and return non-empty markdown."""
    from surogates.tools.builtin.file_ops import _parse_document_to_markdown

    pdf = build_minimal_pdf(tmp_path / "tiny.pdf", heading="Hello PDF")
    md = await _parse_document_to_markdown(pdf)
    assert "Hello PDF" in md


@pytest.mark.asyncio
async def test_parse_docx_returns_markdown(tmp_path: Path) -> None:
    from surogates.tools.builtin.file_ops import _parse_document_to_markdown

    docx = build_minimal_docx(tmp_path / "tiny.docx")
    md = await _parse_document_to_markdown(docx)
    assert "Hello DOCX" in md


@pytest.mark.asyncio
async def test_parse_xlsx_includes_sheet_names(tmp_path: Path) -> None:
    from surogates.tools.builtin.file_ops import _parse_document_to_markdown

    xlsx = build_minimal_xlsx(tmp_path / "tiny.xlsx")
    md = await _parse_document_to_markdown(xlsx)
    assert "Alpha" in md
    assert "Beta" in md


@pytest.mark.asyncio
async def test_parse_pptx_includes_slide_title(tmp_path: Path) -> None:
    from surogates.tools.builtin.file_ops import _parse_document_to_markdown

    pptx = build_minimal_pptx(tmp_path / "tiny.pptx")
    md = await _parse_document_to_markdown(pptx)
    assert "Hello PPTX" in md


@pytest.mark.asyncio
async def test_parser_wraps_markitdown_errors_as_DocumentParseError(
    tmp_path: Path, monkeypatch,
) -> None:
    """Any exception from markitdown must be normalised to DocumentParseError."""
    from surogates.tools.builtin import file_ops
    from surogates.tools.builtin.file_ops import (
        DocumentParseError,
        _parse_document_to_markdown,
    )

    class RaisingMarkItDown:
        def convert(self, *args, **kwargs):
            raise RuntimeError("markitdown said no")

    monkeypatch.setattr(file_ops, "_load_markitdown", lambda: RaisingMarkItDown())

    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"%PDF-1.4 placeholder")
    with pytest.raises(DocumentParseError) as excinfo:
        await _parse_document_to_markdown(bad)
    assert "x.pdf" in str(excinfo.value)
    assert "markitdown said no" in str(excinfo.value)
    # Underlying cause is preserved for telemetry / debugging.
    assert isinstance(excinfo.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_parser_times_out(tmp_path: Path, monkeypatch) -> None:
    """A hung markitdown call must raise DocumentParseError with 'timeout'."""
    from surogates.tools.builtin import file_ops

    pdf = tmp_path / "slow.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder")

    # The fake sleeps longer than the patched timeout but not so long
    # that the orphan executor thread keeps pytest alive after wait_for
    # fires.  asyncio.to_thread cannot cancel the worker thread, so we
    # tune the sleep to ~1s and the timeout to 0.05s.
    class FakeMarkItDown:
        def convert(self, *args, **kwargs):  # noqa: D401
            import time

            time.sleep(1.0)

    monkeypatch.setattr(file_ops, "_DOCUMENT_PARSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        file_ops, "_load_markitdown", lambda: FakeMarkItDown(),
    )

    with pytest.raises(file_ops.DocumentParseError) as excinfo:
        await file_ops._parse_document_to_markdown(pdf)
    assert "timeout" in str(excinfo.value).lower()
