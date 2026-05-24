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
