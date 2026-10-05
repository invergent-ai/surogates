"""Read documents through the file tool, including pagination, caching and errors."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.tools.builtin.file_ops import _read_file_handler
from surogates.tools.utils.document_cache import DocumentCache
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import InProcessRunner
from tests.tools.fixtures.build_documents import (
    build_minimal_docx,
    build_minimal_pdf,
    build_minimal_pptx,
    build_minimal_xlsx,
    build_textless_pdf,
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


def counting(monkeypatch) -> list[bytes]:
    """Each document parsed, as the bytes the parser got: a page of 51 lines."""
    from surogates.tools.builtin import file_ops

    parsed: list[bytes] = []

    async def parse(path: Path) -> str:
        parsed.append(path.read_bytes())
        return "# header\n" + "\n".join(f"line {i}" for i in range(50)) + "\n"

    monkeypatch.setattr(file_ops, "_parse_document_to_text", parse)
    return parsed


def laptop(tmp_path: Path) -> tuple[Path, InProcessRunner]:
    folder = (tmp_path / "laptop").resolve()
    folder.mkdir()
    (folder / "p.pdf").write_bytes(b"%PDF placeholder")
    return folder, InProcessRunner(LocalWorkspaceIO(str(folder)))


@pytest.mark.asyncio
async def test_a_device_documents_next_page_moves_nothing_until_it_changes(
    tmp_path: Path, isolated_document_cache, monkeypatch,
) -> None:
    parsed = counting(monkeypatch)
    folder, runner = laptop(tmp_path)
    wio = DeviceWorkspaceIO(runner, root=str(folder), identity="device:one")
    await _read_file_handler({"path": "p.pdf"}, workspace_io=wio)
    page = json.loads(await _read_file_handler({"path": "p.pdf", "offset": 10, "limit": 5}, workspace_io=wio))
    assert page["content"] == "".join(f"line {i}\n" for i in range(8, 13))
    # Found by the computer, its file and its revision: the second page asked only where it is and its stat.
    assert runner.kinds == ["resolve", "stat", "read", "resolve", "stat"]
    assert parsed == [b"%PDF placeholder"]
    # Another revision is another document.
    (folder / "p.pdf").write_bytes(b"%PDF placeholder, changed")
    await _read_file_handler({"path": "p.pdf"}, workspace_io=wio)
    assert runner.kinds[5:] == ["resolve", "stat", "read"]
    assert parsed == [b"%PDF placeholder", b"%PDF placeholder, changed"]


@pytest.mark.asyncio
async def test_a_device_document_is_found_in_the_cache_by_that_device_only(
    tmp_path: Path, isolated_document_cache, monkeypatch,
) -> None:
    parsed = counting(monkeypatch)
    folder, runner = laptop(tmp_path)
    for device in ("device:one", "device:two"):
        wio = DeviceWorkspaceIO(runner, root=str(folder), identity=device)
        await _read_file_handler({"path": "p.pdf"}, workspace_io=wio)
    assert runner.kinds.count("read") == 2
    assert len(parsed) == 2


@pytest.mark.asyncio
async def test_a_resumed_device_document_read_neither_finds_nor_keeps_a_cache_entry(
    tmp_path: Path, isolated_document_cache, monkeypatch,
) -> None:
    parsed = counting(monkeypatch)
    folder, runner = laptop(tmp_path)
    resumed = DeviceWorkspaceIO(runner, root=str(folder), identity="device:one", caches_documents=False)
    await _read_file_handler({"path": "p.pdf"}, workspace_io=resumed)
    # On an empty cache: it keeps nothing.
    assert list(isolated_document_cache._root.glob("*.md")) == []
    first = DeviceWorkspaceIO(runner, root=str(folder), identity="device:one")
    await _read_file_handler({"path": "p.pdf"}, workspace_io=first)
    await _read_file_handler({"path": "p.pdf"}, workspace_io=resumed)
    # Beside the first run's entry: it finds nothing, and asks for its read, as its first run did.
    assert runner.kinds.count("read") == 3
    assert len(parsed) == 3
    assert len(list(isolated_document_cache._root.glob("*.md"))) == 1


@pytest.mark.asyncio
async def test_a_document_cache_hit_marks_its_entry_used_now_and_leaves_no_lock_file(tmp_path: Path) -> None:
    cache = DocumentCache(root=tmp_path / "doc-cache")

    async def load() -> str:
        return "markdown"

    await cache.get_or_load("a", load)
    [entry] = cache._root.glob("*.md")
    # An atime ahead of the ctime, which a relatime or noatime mount leaves as it is: only the cache moves it.
    os.utime(entry, ns=(time.time_ns() + 3_600 * 10**9, entry.stat().st_mtime_ns))
    before = time.time_ns()
    assert await cache.get_or_load("a", load) == "markdown"
    # Eviction takes the oldest atime, so a document being paged is not the first to go.
    assert before <= entry.stat().st_atime_ns <= time.time_ns()
    assert [p.suffix for p in cache._root.iterdir()] == [".md"]


@pytest.mark.asyncio
async def test_a_cloud_document_keeps_its_cache_key(tmp_path: Path, isolated_document_cache, monkeypatch) -> None:
    counting(monkeypatch)
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF placeholder")
    await _read_file_handler({"path": str(pdf)})
    st = pdf.stat()
    key = hashlib.sha256(f"{pdf.resolve()}|{st.st_mtime_ns}|{st.st_size}|.pdf".encode()).hexdigest()
    assert [entry.name for entry in isolated_document_cache._root.glob("*.md")] == [f"{key}.md"]


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
# OCR only for scanned PDFs + a timeout that isn't held back by the parse
# ---------------------------------------------------------------------------


def _spy_liteparse(monkeypatch, ocr_text: str = "OCR TEXT") -> list[dict]:
    """Record every ``LiteParse(...)`` call's kwargs.

    Non-OCR parses run for real; OCR parses return ``ocr_text`` so the
    test doesn't depend on Tesseract.
    """
    from liteparse import LiteParse
    from liteparse.types import ParseResult

    from surogates.tools.builtin import file_ops

    calls: list[dict] = []

    class SpyLiteParse:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self._kwargs = kwargs

        def parse(self, path_str):
            if self._kwargs.get("ocr_enabled") is False:
                return LiteParse(**self._kwargs).parse(path_str)
            return ParseResult(pages=[], text=ocr_text)

    monkeypatch.setattr(file_ops, "_load_liteparse", lambda: SpyLiteParse)
    return calls


def test_text_layer_pdf_is_parsed_without_ocr(tmp_path: Path, monkeypatch) -> None:
    from surogates.tools.builtin import file_ops

    calls = _spy_liteparse(monkeypatch)
    pdf = build_minimal_pdf(tmp_path / "p.pdf", heading="Hello PDF")

    text = file_ops._convert_to_text_sync(pdf)

    assert "Hello PDF" in text
    assert [c.get("ocr_enabled") for c in calls] == [False]


def test_textless_pdf_is_ocrd_up_to_the_page_cap(tmp_path: Path, monkeypatch) -> None:
    from surogates.tools.builtin import file_ops

    calls = _spy_liteparse(monkeypatch)
    monkeypatch.setattr(file_ops, "_OCR_MAX_PAGES", 2)
    pdf = build_textless_pdf(tmp_path / "scan.pdf", pages=3)

    text = file_ops._convert_to_text_sync(pdf)

    assert [c.get("ocr_enabled") for c in calls] == [False, True]
    assert calls[1]["max_pages"] == 2
    assert "OCR TEXT" in text
    assert "first 2 of 3 pages" in text


def test_thread_parse_timeout_is_not_held_back_by_the_parse(
    tmp_path: Path, monkeypatch,
) -> None:
    """The sandbox child runs tools under ``asyncio.run``; a parse still
    running past the timeout must not delay the timeout error."""
    from surogates.tools.builtin import file_ops

    release = threading.Event()
    monkeypatch.setattr(file_ops, "_use_subprocess_parse", lambda: False)
    monkeypatch.setattr(file_ops, "_DOCUMENT_PARSE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(
        file_ops, "_convert_to_text_sync", lambda path: release.wait(5) and "",
    )

    start = time.monotonic()
    try:
        with pytest.raises(file_ops.DocumentParseError, match="timeout"):
            asyncio.run(file_ops._parse_document_to_text(tmp_path / "slow.pdf"))
        elapsed = time.monotonic() - start
    finally:
        release.set()

    assert elapsed < 2, elapsed
