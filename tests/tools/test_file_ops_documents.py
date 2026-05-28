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
from types import SimpleNamespace

import pytest

from surogates.tools.builtin.file_ops import _read_file_handler
from tests.tools.fixtures.build_documents import (
    build_minimal_docx,
    build_minimal_pdf,
    build_minimal_pptx,
    build_minimal_xlsx,
)


def _fake_liteparse_returning(text: str):
    """Build a fake ``LiteParse`` class whose ``.parse()`` returns ``text``."""

    class FakeLiteParse:
        def __init__(self, **kwargs):
            pass

        def parse(self, path_str):
            return SimpleNamespace(text=text)

    return FakeLiteParse


def _fake_liteparse_raising(exc: Exception):
    """Build a fake ``LiteParse`` class whose ``.parse()`` raises ``exc``."""

    class FakeLiteParse:
        def __init__(self, **kwargs):
            pass

        def parse(self, path_str):
            raise exc

    return FakeLiteParse


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

    We assert on the error envelope shape — not the wording — so the test
    stays green regardless of how the underlying parser surfaces a bad
    placeholder file.
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
# Parser unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_pdf_returns_text(tmp_path: Path) -> None:
    """The parser must invoke liteparse and return non-empty text."""
    from surogates.tools.builtin.file_ops import _parse_document_to_text

    pdf = build_minimal_pdf(tmp_path / "tiny.pdf", heading="Hello PDF")
    text = await _parse_document_to_text(pdf)
    assert "Hello PDF" in text


@pytest.mark.asyncio
async def test_parse_docx_returns_text(tmp_path: Path) -> None:
    from surogates.tools.builtin.file_ops import _parse_document_to_text

    docx = build_minimal_docx(tmp_path / "tiny.docx")
    text = await _parse_document_to_text(docx)
    assert "Hello DOCX" in text


@pytest.mark.asyncio
async def test_parse_xlsx_includes_sheet_names(tmp_path: Path) -> None:
    from surogates.tools.builtin.file_ops import _parse_document_to_text

    xlsx = build_minimal_xlsx(tmp_path / "tiny.xlsx")
    text = await _parse_document_to_text(xlsx)
    assert "Alpha" in text
    assert "Beta" in text


@pytest.mark.asyncio
async def test_parse_pptx_includes_slide_title(tmp_path: Path) -> None:
    from surogates.tools.builtin.file_ops import _parse_document_to_text

    pptx = build_minimal_pptx(tmp_path / "tiny.pptx")
    text = await _parse_document_to_text(pptx)
    assert "Hello PPTX" in text


@pytest.mark.asyncio
async def test_parser_wraps_liteparse_errors_as_DocumentParseError(
    tmp_path: Path, monkeypatch,
) -> None:
    """Any exception from liteparse must be normalised to DocumentParseError."""
    from surogates.tools.builtin import file_ops
    from surogates.tools.builtin.file_ops import (
        DocumentParseError,
        _parse_document_to_text,
    )

    fake = _fake_liteparse_raising(RuntimeError("liteparse said no"))
    monkeypatch.setattr(file_ops, "_load_liteparse", lambda: fake)

    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"%PDF-1.4 placeholder")
    with pytest.raises(DocumentParseError) as excinfo:
        await _parse_document_to_text(bad)
    assert "x.pdf" in str(excinfo.value)
    assert "liteparse said no" in str(excinfo.value)
    # Underlying cause is preserved for telemetry / debugging.
    assert isinstance(excinfo.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_parser_times_out(tmp_path: Path, monkeypatch) -> None:
    """A hung parser call must raise DocumentParseError with 'timeout'."""
    from surogates.tools.builtin import file_ops

    pdf = tmp_path / "slow.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder")

    # The fake sleeps longer than the patched timeout but not so long
    # that the orphan executor thread keeps pytest alive after wait_for
    # fires.  asyncio.to_thread cannot cancel the worker thread, so we
    # tune the sleep to ~1s and the timeout to 0.05s.
    class FakeLiteParse:
        def __init__(self, **kwargs):
            pass

        def parse(self, path_str):
            import time

            time.sleep(1.0)
            return SimpleNamespace(text="")

    monkeypatch.setattr(file_ops, "_DOCUMENT_PARSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(file_ops, "_load_liteparse", lambda: FakeLiteParse)

    with pytest.raises(file_ops.DocumentParseError) as excinfo:
        await file_ops._parse_document_to_text(pdf)
    assert "timeout" in str(excinfo.value).lower()


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
    # Content is line-number-prefixed in the same format as _handle_text.
    assert "50|line 50" in result["content"]
    assert "54|line 54" in result["content"]
    assert "55|line 55" not in result["content"]
    assert result["truncated"] is True
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


# ---------------------------------------------------------------------------
# Tool description advertises native document + image handling
# ---------------------------------------------------------------------------


def test_read_file_description_mentions_native_document_handling() -> None:
    from surogates.tools.builtin.file_ops import READ_FILE_SCHEMA

    desc = READ_FILE_SCHEMA.description.lower()
    assert ".pdf" in desc
    assert ".docx" in desc
    assert ".xlsx" in desc
    assert ".pptx" in desc
    assert "vision" in desc or "image" in desc
    # Negative guard: the old "Cannot read images or binary files" wording
    # would tell the LLM to avoid the tool for these inputs.
    assert "cannot read images" not in desc
