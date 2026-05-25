# Harness native document Read — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the `read_file` tool so PDF/docx/xlsx/pptx files return parsed markdown and image files return a vision-analysis description, all through the same tool the agent already calls.

**Architecture:** Sandbox-side document parsing in `surogates/tools/builtin/file_ops.py` (markitdown backend, file-backed `/tmp` cache). Worker-side image pre-dispatch branch in `surogates/harness/tool_exec.py` that intercepts `read_file` calls on image paths and routes them through `vision_analyze` (worker holds the LLM client; sandbox does not).

**Tech Stack:** Python 3.12, pytest, markitdown (already in sandbox image), pypdf / python-docx / openpyxl / python-pptx (already in sandbox image), asyncio.

**Spec:** [2026-05-24-harness-native-document-read-design.md](../specs/2026-05-24-harness-native-document-read-design.md)

---

## Task tracker

Updated before every commit.

- [x] Task 1 — Extract `_handle_text` and `_apply_line_window` (mechanical refactor)
- [x] Task 2 — Populate `_DOCUMENT_EXTENSIONS`; shrink `BINARY_EXTENSIONS`
- [x] Task 3 — Add parser deps + `DocumentParseError` + `_parse_document_to_markdown`
- [x] Task 4 — File-backed `_document_cache` under `/tmp`
- [x] Task 5 — Wire `_handle_document` to cache + parser
- [x] Task 6 — Update the `read_file` tool description
- [x] Task 7 — Worker-side image pre-dispatch branch
- [x] Task 8 — Integration test for documents
- [x] Task 9 — Verify dependency lock and parser availability
- [x] Task 10 — Update `CLAUDE.md`
- [x] Task 11 — Smoke test the whole stack and verify
- [x] Task 12 — Swap PDF backend to pymupdf4llm (markitdown linearises tables; pymupdf4llm emits real markdown tables)

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| [surogates/tools/builtin/file_ops.py](../../../surogates/tools/builtin/file_ops.py) | `_read_file_handler` reshaped as dispatcher; new `_handle_text`, `_handle_document`, `_apply_line_window`, `_DOCUMENT_EXTENSIONS`, `_parse_document_to_markdown`, `DocumentParseError`, `_document_cache` | Modify |
| [surogates/tools/utils/binary_extensions.py](../../../surogates/tools/utils/binary_extensions.py) | Remove `.docx/.xlsx/.pptx` from `BINARY_EXTENSIONS` | Modify (3 lines) |
| [surogates/tools/utils/document_cache.py](../../../surogates/tools/utils/document_cache.py) | New: file-backed LRU under `/tmp/surogates-read-cache/documents/` | Create |
| [surogates/harness/tool_exec.py](../../../surogates/harness/tool_exec.py) | New worker-side image pre-dispatch branch before sandbox dispatch (~line 1214) | Modify |
| [surogates/harness/image_read.py](../../../surogates/harness/image_read.py) | New: worker-side in-memory image cache + `read_file`-shaped result renderer | Create |
| [tests/tools/__init__.py](../../../tests/tools/__init__.py) | Empty package marker | Create |
| [tests/tools/test_file_ops_documents.py](../../../tests/tools/test_file_ops_documents.py) | Unit tests for document path: refactor regression + markitdown happy path + cache + errors | Create |
| [tests/tools/test_file_ops_images_via_worker.py](../../../tests/tools/test_file_ops_images_via_worker.py) | Unit tests for the worker image pre-dispatch branch | Create |
| [tests/integration/test_read_document_e2e.py](../../../tests/integration/test_read_document_e2e.py) | In-process workspace round-trip for PDF/docx/xlsx/pptx via `read_file` | Create |
| [tests/tools/test_document_cache.py](../../../tests/tools/test_document_cache.py) | Cache file layout, mtime invalidation, LRU eviction, lock contention | Create |
| [tests/tools/fixtures/build_documents.py](../../../tests/tools/fixtures/build_documents.py) | Programmatic fixture builders for tiny .pdf/.docx/.xlsx/.pptx | Create |
| [pyproject.toml](../../../pyproject.toml) | Add `markitdown[pptx]` to project deps and `reportlab` to dev deps so harness-local parser tests can run | Modify |
| [CLAUDE.md](../../../CLAUDE.md) | Note that `read_file` now handles PDF/Office/images natively | Modify |
| [images/sandbox/Dockerfile](../../../images/sandbox/Dockerfile) | No code change. Production rollout requires rebuilding this image; called out in commit message | Reference only |

---

## Task 1: Extract `_handle_text` and `_apply_line_window` (mechanical refactor)

Pure function extraction. No behavior change. Existing tests must stay green; we add a snapshot regression test for safety.

**Files:**
- Modify: [surogates/tools/builtin/file_ops.py:804-1032](../../../surogates/tools/builtin/file_ops.py)
- Create: `tests/tools/__init__.py`
- Create: `tests/tools/test_file_ops_documents.py`

- [ ] **Step 1: Add the empty package marker so tests can be collected**

```bash
mkdir -p /work/surogates/tests/tools
touch /work/surogates/tests/tools/__init__.py
```

- [ ] **Step 2: Write the failing snapshot regression test**

Create `/work/surogates/tests/tools/test_file_ops_documents.py`:

```python
"""Tests for document-aware read_file behavior.

Layered as:
  - Regression snapshot for the text path (proves the refactor is mechanical).
  - Document-format unit tests (PDF / docx / xlsx / pptx).
  - Cache behavior tests.
  - Error envelope tests.
"""

from __future__ import annotations

import json
import os
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
```

- [ ] **Step 3: Run it to confirm the existing handler still passes this contract**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py::test_text_path_unchanged_after_refactor -v
```

Expected: PASS (we haven't refactored yet — this locks in the current behavior so the refactor in Step 4 can't drift).

- [ ] **Step 4: Refactor `_read_file_handler` into a dispatcher + `_handle_text` + `_apply_line_window`**

In `/work/surogates/surogates/tools/builtin/file_ops.py`, locate `async def _read_file_handler` (~line 804) and replace it with the following block. Order matters for readability: constants and helpers first, then dispatcher, then handlers. The body of `_handle_text` is the original handler body lifted verbatim; the only change is that line-windowing now goes through `_apply_line_window`.

```python
# Populated in Task 2.
_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset()


def _apply_line_window(
    lines: list[str],
    offset: int,
    limit: int,
) -> tuple[list[str], int, int, int, bool]:
    """Slice ``lines`` into a 1-indexed window.

    Returns ``(selected, total_lines, start_idx, end_idx, truncated)``.
    Shared by ``_handle_text`` and ``_handle_document`` so both paths
    paginate identically.
    """
    total_lines = len(lines)
    start_idx = offset - 1
    end_idx = min(start_idx + limit, total_lines)
    selected = lines[start_idx:end_idx]
    truncated = end_idx < total_lines
    return selected, total_lines, start_idx, end_idx, truncated


async def _read_file_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Dispatcher: route by file extension, then delegate to a handler.

    image extensions never reach this handler in production; the worker
    pre-dispatch branch in tool_exec.py intercepts them before sandbox
    dispatch.  We still keep the image-redirect error here as a defensive
    fallback for harness-local testing where the worker branch is bypassed.
    """
    path = arguments.get("path", "")
    workspace_path = kwargs.get("workspace_path")

    if not path:
        return _tool_error("No path provided")

    try:
        if _is_blocked_device(path):
            return json.dumps({
                "error": (
                    f"Cannot read '{path}': this is a device file that would "
                    "block or produce infinite output."
                ),
            })

        resolved = Path(_resolve_user_path(path, workspace_path))
        ext = resolved.suffix.lower()

        if _is_image(str(resolved)):
            # Defensive fallback only; production goes through the worker
            # branch in tool_exec.py before this handler is reached.
            return json.dumps({
                "error": (
                    f"Image file detected: '{path}'. "
                    "Use vision_analyze with this file path to inspect the "
                    "image contents."
                ),
            })

        if ext in _DOCUMENT_EXTENSIONS:
            return await _handle_document(path, resolved, arguments, **kwargs)

        if has_binary_extension(str(resolved)):
            return json.dumps({
                "error": (
                    f"Cannot read binary file '{path}' ({ext}). "
                    "Use vision_analyze for images, or terminal to inspect "
                    "binary files."
                ),
            })

        return await _handle_text(path, resolved, arguments, **kwargs)
    except Exception as exc:
        return _tool_error(str(exc))


async def _handle_document(
    path: str,
    resolved: Path,
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Stub — implemented in Task 5."""
    raise NotImplementedError("_handle_document implemented in Task 5")


async def _handle_text(
    path: str,
    resolved: Path,
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """UTF-8 / BOM / encoding text reader. Lifted verbatim from the
    pre-refactor ``_read_file_handler`` body.
    """
    offset = max(arguments.get("offset", 1), 1)
    limit = min(arguments.get("limit", 500), get_max_lines())
    task_id = kwargs.get("task_id", "default")
    resolved_str = str(resolved)

    # ── Dedup check ───────────────────────────────────────────────
    dedup_key = (resolved_str, offset, limit)
    task_data = _init_task_data(task_id)
    with _read_tracker_lock:
        cached_mtime = task_data.get("dedup", {}).get(dedup_key)

    if cached_mtime is not None:
        try:
            current_mtime = os.path.getmtime(resolved_str)
            if current_mtime == cached_mtime:
                return json.dumps({
                    "content": (
                        "File unchanged since last read. The content from "
                        "the earlier read_file result in this conversation is "
                        "still current — refer to that instead of re-reading."
                    ),
                    "path": path,
                    "dedup": True,
                }, ensure_ascii=False)
        except OSError:
            pass

    if not os.path.exists(resolved_str):
        result_dict: dict[str, Any] = {"error": f"File not found: {path}"}
        similar = _suggest_similar_files(path)
        if similar:
            result_dict["similar_files"] = similar
            result_dict["hint"] = (
                "Did you mean one of these files? "
                + ", ".join(similar)
            )
        return json.dumps(result_dict, ensure_ascii=False)

    file_size = os.path.getsize(resolved_str)

    encoding = "utf-8"
    try:
        with open(resolved_str, "rb") as fb:
            raw_head = fb.read(8192)
        if raw_head.startswith(b"\xff\xfe\x00\x00"):
            encoding = "utf-32-le"
        elif raw_head.startswith(b"\x00\x00\xfe\xff"):
            encoding = "utf-32-be"
        elif raw_head.startswith(b"\xff\xfe"):
            encoding = "utf-16-le"
        elif raw_head.startswith(b"\xfe\xff"):
            encoding = "utf-16-be"
        elif raw_head.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        else:
            if b"\x00" in raw_head:
                return json.dumps({
                    "error": (
                        f"Cannot read binary file '{path}'. "
                        "Use vision_analyze for images, or terminal to "
                        "inspect binary files."
                    ),
                })
    except OSError:
        pass

    try:
        with open(resolved_str, encoding=encoding, errors="replace") as fh:
            lines = fh.readlines()
    except (OSError, UnicodeDecodeError) as exc:
        return _tool_error(f"Failed to read file: {exc}")

    selected, total_lines, _start_idx, end_idx, truncated = _apply_line_window(
        lines, offset, limit,
    )

    content = ""
    for i, line in enumerate(selected, start=offset):
        content += f"{i}|{line}"

    content_len = len(content)
    max_chars = get_max_bytes()
    if content_len > max_chars:
        return json.dumps({
            "error": (
                f"Read produced {content_len:,} characters which exceeds "
                f"the safety limit ({max_chars:,} chars). "
                "Use offset and limit to read a smaller range. "
                f"The file has {total_lines} lines total."
            ),
            "path": path,
            "total_lines": total_lines,
            "file_size": file_size,
        }, ensure_ascii=False)

    result_dict = {
        "content": content,
        "path": path,
        "total_lines": total_lines,
        "lines_shown": len(selected),
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "file_size": file_size,
    }

    if (file_size and file_size > _LARGE_FILE_HINT_BYTES
            and limit > 200
            and truncated):
        result_dict["_hint"] = (
            f"This file is large ({file_size:,} bytes). "
            "Consider reading only the section you need with offset and "
            "limit to keep context usage efficient."
        )

    read_key = ("read", path, offset, limit)
    with _read_tracker_lock:
        task_data["read_history"].add((path, offset, limit))
        if task_data["last_key"] == read_key:
            task_data["consecutive"] += 1
        else:
            task_data["last_key"] = read_key
            task_data["consecutive"] = 1
        count = task_data["consecutive"]
        try:
            _mtime_now = os.path.getmtime(resolved_str)
            task_data["dedup"][dedup_key] = _mtime_now
            task_data.setdefault("read_timestamps", {})[resolved_str] = _mtime_now
            _cap_read_tracker_data(task_data)
        except OSError:
            pass

    if count >= 4:
        return json.dumps({
            "error": (
                f"BLOCKED: You have read this exact file region {count} "
                "times in a row. The content has NOT changed. You already "
                "have this information. STOP re-reading and proceed with "
                "your task."
            ),
            "path": path,
            "already_read": count,
        }, ensure_ascii=False)
    elif count >= 3:
        result_dict["_warning"] = (
            f"You have read this exact file region {count} times "
            "consecutively. The content has not changed since your last "
            "read. Use the information you already have. If you are stuck "
            "in a loop, stop reading and proceed with writing or responding."
        )

    return json.dumps(result_dict, ensure_ascii=False)
```

- [ ] **Step 5: Run the regression test + the existing test suite**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py tests/test_file_ops_tracker.py -v
```

Expected: PASS (text path identical; tracker test still green).

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/tools/builtin/file_ops.py tests/tools/__init__.py tests/tools/test_file_ops_documents.py
git commit -m "refactor(file_ops): extract _handle_text and _apply_line_window

Mechanical extraction in preparation for document parsing. No behavior
change; locked by snapshot regression test."
```

---

## Task 2: Populate `_DOCUMENT_EXTENSIONS`; shrink `BINARY_EXTENSIONS`

Tiny change. Documents are now recognized by the dispatcher but the document handler still raises NotImplementedError — that's intentional. Tests for the format-specific behavior come in Task 5.

**Files:**
- Modify: `surogates/tools/builtin/file_ops.py` — set `_DOCUMENT_EXTENSIONS`
- Modify: `surogates/tools/utils/binary_extensions.py` — drop `.docx/.xlsx/.pptx`
- Modify: `tests/tools/test_file_ops_documents.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/tools/test_file_ops_documents.py`:

```python
@pytest.mark.asyncio
async def test_pdf_routed_to_document_handler(tmp_path: Path) -> None:
    """A .pdf path must reach _handle_document, not the binary-error path.

    Until Task 5 ships, _handle_document raises NotImplementedError,
    which the outer try/except converts into a generic tool error.  We
    assert on the error envelope shape *not* the wording, so this test
    stays green once Task 5 replaces the stub.
    """
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 placeholder")
    result_json = await _read_file_handler({"path": str(src)})
    result = json.loads(result_json)
    # Must NOT contain the binary-extension error message.
    assert "Cannot read binary file" not in result.get("error", "")


@pytest.mark.asyncio
async def test_docx_no_longer_blocked_as_binary(tmp_path: Path) -> None:
    src = tmp_path / "doc.docx"
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
```

- [ ] **Step 2: Run to confirm it fails**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py -v -k "pdf_routed or docx_no_longer or legacy_doc_still"
```

Expected: `test_docx_no_longer_blocked_as_binary` FAILs (docx is currently in BINARY_EXTENSIONS). The PDF test may already pass (pdf isn't in BINARY_EXTENSIONS). The legacy_doc test passes.

- [ ] **Step 3: Set `_DOCUMENT_EXTENSIONS`**

In `surogates/tools/builtin/file_ops.py`, replace:

```python
_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset()  # populated in Task 2
```

with:

```python
_DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
})
```

- [ ] **Step 4: Shrink `BINARY_EXTENSIONS`**

In `surogates/tools/utils/binary_extensions.py`, replace line 21-23:

```python
    # Documents (exclude .pdf -- text-based, agents may want to inspect)
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
```

with:

```python
    # Legacy binary documents only.  .pdf/.docx/.xlsx/.pptx are handled
    # natively by read_file via markitdown.
    ".doc", ".xls", ".ppt",
    ".odt", ".ods", ".odp",
```

- [ ] **Step 5: Run all the doc-discrimination tests**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py -v
```

Expected: All three tests PASS.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/tools/builtin/file_ops.py surogates/tools/utils/binary_extensions.py tests/tools/test_file_ops_documents.py
git commit -m "feat(file_ops): recognize .pdf/.docx/.xlsx/.pptx as documents

Removes office formats from the binary blocklist and routes them to the
(still-stubbed) document handler.  Legacy .doc/.xls/.ppt remain binary."
```

---

## Task 3: Add parser dependencies, `DocumentParseError`, and `_parse_document_to_markdown`

Lazy-import wrapper around markitdown with a 30 s timeout. No cache yet.

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `surogates/tools/builtin/file_ops.py`
- Modify: `tests/tools/test_file_ops_documents.py`
- Create: `tests/tools/fixtures/build_documents.py`

- [ ] **Step 1: Add local dev/test parser dependencies first**

The sandbox image already installs `markitdown[pptx]`, `pypdf`,
`python-docx`, `openpyxl`, `python-pptx`, and `pandoc`, but this plan's unit
tests run in the local worker/dev environment via `uv run pytest`. Add the
Python packages before writing parser tests, otherwise Task 3 cannot pass
locally.

In `/work/surogates/pyproject.toml`, add:

```toml
# project dependencies
"markitdown[pptx]>=0.0.1",
```

In `[dependency-groups].dev`, add:

```toml
"reportlab>=4.0",
```

Then sync:

```bash
cd /work/surogates && uv sync
```

- [ ] **Step 2: Write the fixture builder**

Create `/work/surogates/tests/tools/fixtures/__init__.py` (empty) and `/work/surogates/tests/tools/fixtures/build_documents.py`:

```python
"""Programmatic builders for tiny office documents used in tests.

We generate fixtures at test time rather than checking binaries into git.
"""

from __future__ import annotations

from pathlib import Path


def build_minimal_pdf(path: Path, heading: str = "Hello PDF") -> Path:
    """Write a minimal 2-page PDF with a known heading on each page."""
    # Use reportlab so the fixture contains a real extractable text stream.
    try:
        from reportlab.pdfgen import canvas  # type: ignore
        c = canvas.Canvas(str(path))
        c.setFont("Helvetica", 14)
        c.drawString(72, 720, heading)
        c.showPage()
        c.setFont("Helvetica", 14)
        c.drawString(72, 720, heading + " page 2")
        c.showPage()
        c.save()
        return path
    except ImportError:
        raise RuntimeError(
            "Test fixture builder requires 'reportlab' for PDF generation. "
            "Add reportlab to the test dependency group."
        )


def build_minimal_docx(path: Path) -> Path:
    """Write a docx with one H1, one bullet, one 2x2 table."""
    from docx import Document
    doc = Document()
    doc.add_heading("Hello DOCX", level=1)
    doc.add_paragraph("first bullet", style="List Bullet")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "A"
    table.cell(0, 1).text = "B"
    table.cell(1, 0).text = "1"
    table.cell(1, 1).text = "2"
    doc.save(str(path))
    return path


def build_minimal_xlsx(path: Path) -> Path:
    """Write an xlsx with two sheets."""
    from openpyxl import Workbook
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Alpha"
    ws1["A1"] = "name"
    ws1["B1"] = "value"
    ws1["A2"] = "x"
    ws1["B2"] = 1
    ws2 = wb.create_sheet("Beta")
    ws2["A1"] = "name"
    ws2["A2"] = "y"
    wb.save(str(path))
    return path


def build_minimal_pptx(path: Path) -> Path:
    """Write a pptx with one slide whose body says 'Hello PPTX'."""
    from pptx import Presentation
    prs = Presentation()
    slide_layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(slide_layout)
    slide.shapes.title.text = "Hello PPTX"
    slide.placeholders[1].text = "body text"
    prs.save(str(path))
    return path
```

- [ ] **Step 3: Write the failing unit test for the parser**

Append to `tests/tools/test_file_ops_documents.py`:

```python
from tests.tools.fixtures.build_documents import (
    build_minimal_docx,
    build_minimal_pdf,
    build_minimal_pptx,
    build_minimal_xlsx,
)


@pytest.mark.asyncio
async def test_parse_pdf_returns_markdown(tmp_path: Path) -> None:
    """The parser must invoke markitdown and return non-empty markdown."""
    from surogates.tools.builtin.file_ops import _parse_document_to_markdown

    pdf = build_minimal_pdf(tmp_path / "tiny.pdf", heading="Hello PDF")
    md = await _parse_document_to_markdown(pdf)
    assert "Hello PDF" in md


@pytest.mark.asyncio
async def test_parser_raises_DocumentParseError_on_corrupt_input(
    tmp_path: Path,
) -> None:
    from surogates.tools.builtin.file_ops import (
        DocumentParseError,
        _parse_document_to_markdown,
    )

    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"not actually a pdf")
    with pytest.raises(DocumentParseError) as excinfo:
        await _parse_document_to_markdown(bad)
    assert "corrupt.pdf" in str(excinfo.value)


@pytest.mark.asyncio
async def test_parser_times_out_after_30s(tmp_path: Path, monkeypatch) -> None:
    """A hung markitdown call must raise DocumentParseError with 'timeout'."""
    from surogates.tools.builtin import file_ops

    pdf = tmp_path / "slow.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder")

    def hung_convert(*args, **kwargs):
        import time
        time.sleep(60)  # longer than the 30s timeout

    class FakeMarkItDown:
        def convert(self, *args, **kwargs):
            hung_convert()

    # Speed up the test: patch the timeout constant down.
    monkeypatch.setattr(file_ops, "_DOCUMENT_PARSE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(
        file_ops, "_load_markitdown", lambda: FakeMarkItDown(),
    )

    with pytest.raises(file_ops.DocumentParseError) as excinfo:
        await file_ops._parse_document_to_markdown(pdf)
    assert "timeout" in str(excinfo.value).lower()
```

- [ ] **Step 4: Run to confirm it fails**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py -v -k "parse_pdf or DocumentParseError or times_out"
```

Expected: FAIL with `ImportError` / `AttributeError` (the parser doesn't exist yet).

- [ ] **Step 5: Implement the parser**

In `surogates/tools/builtin/file_ops.py`, add near the top of the file (after the imports, before `_EXPECTED_WRITE_ERRNOS`):

```python
import asyncio

_DOCUMENT_PARSE_TIMEOUT_S: float = 30.0


class DocumentParseError(Exception):
    """Raised when markitdown fails to convert a document.

    Carries the file path and the underlying error message so the
    tool-error envelope can surface a useful fallback hint.
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Could not parse {path}: {reason}")


def _load_markitdown() -> Any:
    """Lazy-import markitdown so worker startup does not require it.

    The worker process imports file_ops to register tool schemas, but
    markitdown is only installed inside the sandbox image.  Importing it
    at module load time would break worker startup.
    """
    from markitdown import MarkItDown  # noqa: PLC0415
    return MarkItDown()


def _convert_to_markdown_sync(path: Path) -> str:
    """Sync entry point for ``asyncio.to_thread``."""
    md = _load_markitdown()
    result = md.convert(str(path))
    return result.text_content or ""


async def _parse_document_to_markdown(path: Path) -> str:
    """Convert a document to markdown via markitdown.

    Wrapped in ``asyncio.to_thread`` (markitdown is sync) and bounded by
    ``_DOCUMENT_PARSE_TIMEOUT_S`` wall-clock.  Any exception, including
    timeout, is normalised to :class:`DocumentParseError`.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_convert_to_markdown_sync, path),
            timeout=_DOCUMENT_PARSE_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        raise DocumentParseError(
            path,
            f"parse timeout after {_DOCUMENT_PARSE_TIMEOUT_S}s",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise DocumentParseError(path, str(exc)) from exc
```

- [ ] **Step 6: Run all parser tests**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py -v -k "parse_pdf or DocumentParseError or times_out"
```

Expected: PASS for all three.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add pyproject.toml uv.lock surogates/tools/builtin/file_ops.py tests/tools/test_file_ops_documents.py tests/tools/fixtures/
git commit -m "feat(file_ops): add _parse_document_to_markdown with timeout

Lazy-imports markitdown so the worker (which lacks markitdown) does not
break at startup.  asyncio.wait_for caps parses at 30s; failures and
timeouts surface as DocumentParseError carrying the path + reason."
```

---

## Task 4: File-backed `_document_cache` under `/tmp`

Implements the LRU described in the spec. Lives in its own module so it can be unit-tested independently of file_ops.

**Files:**
- Create: `surogates/tools/utils/document_cache.py`
- Create: `tests/tools/test_document_cache.py`

- [ ] **Step 1: Write the failing test**

Create `/work/surogates/tests/tools/test_document_cache.py`:

```python
"""Tests for the file-backed document cache."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def cache(tmp_path: Path):
    from surogates.tools.utils.document_cache import DocumentCache
    return DocumentCache(
        root=tmp_path / "cache",
        max_entries=3,
        max_entry_bytes=1024,
    )


@pytest.mark.asyncio
async def test_miss_then_hit_does_not_reparse(cache, tmp_path: Path) -> None:
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"original")

    calls = {"n": 0}

    async def parse(path: Path) -> str:
        calls["n"] += 1
        return f"markdown for {path.name}"

    md1 = await cache.get_or_parse(src, parse)
    md2 = await cache.get_or_parse(src, parse)
    assert md1 == "markdown for doc.pdf"
    assert md2 == md1
    assert calls["n"] == 1, "second call should be a cache hit"


@pytest.mark.asyncio
async def test_mtime_change_invalidates(cache, tmp_path: Path) -> None:
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"v1")

    counter = {"n": 0}

    async def parse(path: Path) -> str:
        counter["n"] += 1
        return f"version {counter['n']}: {path.read_bytes().decode()}"

    md1 = await cache.get_or_parse(src, parse)
    # Force mtime to advance.
    time.sleep(0.01)
    src.write_bytes(b"v2")
    md2 = await cache.get_or_parse(src, parse)
    assert md1 != md2
    assert counter["n"] == 2


@pytest.mark.asyncio
async def test_oversized_markdown_not_cached(cache, tmp_path: Path) -> None:
    src = tmp_path / "big.pdf"
    src.write_bytes(b"raw")
    huge = "x" * 2048  # max_entry_bytes is 1024 in the fixture

    calls = {"n": 0}

    async def parse(path: Path) -> str:
        calls["n"] += 1
        return huge

    md1 = await cache.get_or_parse(src, parse)
    md2 = await cache.get_or_parse(src, parse)
    assert md1 == huge
    assert md2 == huge
    # Both calls re-parsed because the result was too large to cache.
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_lru_evicts_oldest(cache, tmp_path: Path) -> None:
    """Cache size is 3.  Inserting 4 distinct files must evict the first."""
    files = []
    for i in range(4):
        f = tmp_path / f"f{i}.pdf"
        f.write_bytes(b"x")
        files.append(f)

    calls = {"n": 0}

    async def parse(path: Path) -> str:
        calls["n"] += 1
        return path.name

    for f in files:
        await cache.get_or_parse(f, parse)
    # f0 was evicted; re-reading it must re-parse.
    n_before = calls["n"]
    await cache.get_or_parse(files[0], parse)
    assert calls["n"] == n_before + 1


@pytest.mark.asyncio
async def test_concurrent_same_process_read_parses_once(cache, tmp_path: Path) -> None:
    """The in-process async lock should collapse duplicate concurrent misses."""
    import asyncio

    src = tmp_path / "doc.pdf"
    src.write_bytes(b"raw")
    calls = {"n": 0}

    async def parse(path: Path) -> str:
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return "markdown"

    one, two = await asyncio.gather(
        cache.get_or_parse(src, parse),
        cache.get_or_parse(src, parse),
    )
    assert one == two == "markdown"
    assert calls["n"] == 1
```

- [ ] **Step 2: Run to confirm it fails**

```bash
cd /work/surogates && uv run pytest tests/tools/test_document_cache.py -v
```

Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `DocumentCache`**

Create `/work/surogates/surogates/tools/utils/document_cache.py`:

```python
"""File-backed LRU cache for parsed documents.

The K8s sandbox spawns a fresh ``tool-executor`` Python process per
tool call, so an in-memory cache would be empty on every subsequent
``read_file``.  Persisting to ``/tmp`` (pod-local) survives across
exec calls within the same pod while staying out of the user workspace.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_CACHE_ROOT = Path("/tmp/surogates-read-cache/documents")
DEFAULT_MAX_ENTRIES = 8
DEFAULT_MAX_ENTRY_BYTES = 2 * 1024 * 1024  # 2 MB


class DocumentCache:
    """LRU keyed on ``(abs_path, mtime_ns, size, ext)``.

    Eviction policy: at insert time, if the directory holds more than
    ``max_entries`` cache files, the entry with the oldest atime is
    deleted.  Reads update the atime via ``os.utime`` so LRU works on
    filesystems that mount with ``noatime``.
    """

    def __init__(
        self,
        root: Path = DEFAULT_CACHE_ROOT,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    ) -> None:
        self._root = root
        self._max_entries = max_entries
        self._max_entry_bytes = max_entry_bytes
        self._root.mkdir(parents=True, exist_ok=True)
        self._inflight_lock = asyncio.Lock()

    def _key(self, source: Path) -> str:
        st = source.stat()
        ext = source.suffix.lower()
        raw = f"{source.resolve()}|{st.st_mtime_ns}|{st.st_size}|{ext}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _entry_path(self, key: str) -> Path:
        return self._root / f"{key}.md"

    def _lock_path(self, key: str) -> Path:
        return self._root / f"{key}.lock"

    async def get_or_parse(
        self,
        source: Path,
        parse: Callable[[Path], Awaitable[str]],
    ) -> str:
        """Return cached markdown for ``source`` or call ``parse`` and store."""
        try:
            key = self._key(source)
        except OSError as exc:
            logger.debug("cache key stat failed for %s: %s", source, exc)
            return await parse(source)

        entry = self._entry_path(key)
        if entry.exists():
            try:
                content = entry.read_text(encoding="utf-8")
                # Bump atime so LRU eviction prefers other entries.
                st = entry.stat()
                os.utime(entry, (time.time(), st.st_mtime))
                return content
            except OSError as exc:
                logger.debug("cache read failed for %s: %s", entry, exc)

        # Miss — parse, then persist if small enough.
        async with self._inflight_lock:
            # Double-check after acquiring the in-process lock; another
            # coroutine may have populated the file while we waited.
            if entry.exists():
                try:
                    return entry.read_text(encoding="utf-8")
                except OSError:
                    pass

            markdown = await parse(source)
            self._maybe_store(key, markdown)
            return markdown

    def _maybe_store(self, key: str, markdown: str) -> None:
        encoded = markdown.encode("utf-8")
        if len(encoded) > self._max_entry_bytes:
            logger.debug(
                "skipping cache write for %s: %d bytes > limit %d",
                key, len(encoded), self._max_entry_bytes,
            )
            return

        entry = self._entry_path(key)
        tmp = entry.with_suffix(".tmp")
        lock_file = self._lock_path(key)

        # Best-effort cross-process lock.  We hold a file lock only while
        # the rename happens, not while parsing.
        try:
            with open(lock_file, "w") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    tmp.write_bytes(encoded)
                    os.replace(tmp, entry)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            logger.debug("cache write failed for %s: %s", entry, exc)
            return

        self._evict_if_full()

    def _evict_if_full(self) -> None:
        entries = sorted(
            (p for p in self._root.iterdir() if p.suffix == ".md"),
            key=lambda p: p.stat().st_atime,
        )
        # Keep the newest ``max_entries``; delete the older ones.
        for old in entries[: max(0, len(entries) - self._max_entries)]:
            try:
                old.unlink()
            except OSError:
                pass


def default_cache() -> DocumentCache:
    """Return the process-wide default cache, creating it lazily."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = DocumentCache()
    return _DEFAULT


_DEFAULT: DocumentCache | None = None
```

- [ ] **Step 4: Run the cache tests**

```bash
cd /work/surogates && uv run pytest tests/tools/test_document_cache.py -v
```

Expected: All four PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/tools/utils/document_cache.py tests/tools/test_document_cache.py
git commit -m "feat(tools): file-backed LRU document cache under /tmp

Persists across tool-executor process restarts in the same K8s pod.
Keyed on (abs_path, mtime_ns, size, ext); LRU eviction by atime;
fcntl flock guards cross-process writes."
```

---

## Task 5: Wire `_handle_document` to the cache + parser

This is the task that makes the agent actually see parsed markdown.

**Files:**
- Modify: `surogates/tools/builtin/file_ops.py`
- Modify: `tests/tools/test_file_ops_documents.py`

- [ ] **Step 1: Write the failing happy-path tests**

Append to `tests/tools/test_file_ops_documents.py`:

```python
@pytest.mark.asyncio
async def test_read_pdf_returns_markdown_via_handler(tmp_path: Path) -> None:
    pdf = build_minimal_pdf(tmp_path / "p.pdf", heading="Hello PDF")
    result_json = await _read_file_handler({"path": str(pdf)})
    result = json.loads(result_json)
    assert "error" not in result, result
    assert "Hello PDF" in result["content"]
    assert result["total_lines"] > 0
    assert result["truncated"] is False
    assert result["path"] == str(pdf)


@pytest.mark.asyncio
async def test_read_docx_returns_markdown_via_handler(tmp_path: Path) -> None:
    docx = build_minimal_docx(tmp_path / "d.docx")
    result_json = await _read_file_handler({"path": str(docx)})
    result = json.loads(result_json)
    assert "error" not in result, result
    assert "Hello DOCX" in result["content"]


@pytest.mark.asyncio
async def test_read_xlsx_includes_both_sheet_names(tmp_path: Path) -> None:
    xlsx = build_minimal_xlsx(tmp_path / "x.xlsx")
    result_json = await _read_file_handler({"path": str(xlsx)})
    result = json.loads(result_json)
    assert "error" not in result, result
    # markitdown labels sheets with their names; at minimum both should
    # appear in the rendered markdown.
    assert "Alpha" in result["content"]
    assert "Beta" in result["content"]


@pytest.mark.asyncio
async def test_read_pptx_includes_slide_text(tmp_path: Path) -> None:
    pptx = build_minimal_pptx(tmp_path / "p.pptx")
    result_json = await _read_file_handler({"path": str(pptx)})
    result = json.loads(result_json)
    assert "error" not in result, result
    assert "Hello PPTX" in result["content"]


@pytest.mark.asyncio
async def test_pagination_via_offset_limit(tmp_path: Path, monkeypatch) -> None:
    """offset=N + limit=M slices the rendered markdown by 1-indexed lines."""
    from surogates.tools.builtin import file_ops

    fake_md = "\n".join(f"line {i}" for i in range(1, 101)) + "\n"

    async def fake_parse(path: Path) -> str:
        return fake_md

    monkeypatch.setattr(file_ops, "_parse_document_to_markdown", fake_parse)

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF placeholder")

    result_json = await _read_file_handler(
        {"path": str(pdf), "offset": 50, "limit": 5},
    )
    result = json.loads(result_json)
    # Content is line-number-prefixed in the same format as _handle_text.
    assert "50|line 50" in result["content"]
    assert "54|line 54" in result["content"]
    assert "55|line 55" not in result["content"]
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_corrupt_pdf_returns_fallback_hint(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"not a pdf")
    result_json = await _read_file_handler({"path": str(bad)})
    result = json.loads(result_json)
    assert "error" in result
    # Must mention at least one subprocess fallback.
    err = result["error"].lower()
    assert "pdftotext" in err or "pandoc" in err


@pytest.mark.asyncio
async def test_document_cache_hit_skips_reparse(
    tmp_path: Path, monkeypatch,
) -> None:
    from surogates.tools.builtin import file_ops

    calls = {"n": 0}
    async def counting_parse(path: Path) -> str:
        calls["n"] += 1
        return "# header\n" + "\n".join(f"line {i}" for i in range(50))

    monkeypatch.setattr(file_ops, "_parse_document_to_markdown", counting_parse)

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF placeholder")

    # First read populates the cache.
    await _read_file_handler({"path": str(pdf)})
    # Different window — must hit the cache, not re-parse.
    await _read_file_handler({"path": str(pdf), "offset": 10, "limit": 5})
    assert calls["n"] == 1
```

- [ ] **Step 2: Run to confirm they fail**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py -v -k "via_handler or sheet_names or slide_text or offset_limit or fallback_hint or cache_hit_skips"
```

Expected: FAIL (NotImplementedError from the stub).

- [ ] **Step 3: Implement `_handle_document`**

In `surogates/tools/builtin/file_ops.py`, replace the stub `_handle_document` with:

```python
async def _handle_document(
    path: str,
    resolved: Path,
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Read a PDF / docx / xlsx / pptx as markdown.

    Pagination uses the same ``_apply_line_window`` semantics as
    ``_handle_text``: ``offset`` is 1-indexed, ``limit`` caps the line
    count, and lines are emitted with ``"{lineno}|{content}"`` prefixes.
    """
    from surogates.tools.utils.document_cache import default_cache

    offset = max(arguments.get("offset", 1), 1)
    limit = min(arguments.get("limit", 500), get_max_lines())

    if not resolved.exists():
        return json.dumps({"error": f"File not found: {path}"}, ensure_ascii=False)
    if resolved.is_dir():
        return _tool_error(f"Failed to read file: {resolved} is a directory")

    file_size = resolved.stat().st_size
    try:
        markdown = await default_cache().get_or_parse(
            resolved, _parse_document_to_markdown,
        )
    except DocumentParseError as exc:
        ext = resolved.suffix.lower().lstrip(".")
        return _tool_error(
            f"Could not parse {path} as a {ext} document: {exc.reason}. "
            "You can retry with a subprocess fallback: try running "
            "`pdftotext`, `pandoc`, or a Python script using "
            "pypdf/python-docx/openpyxl (all pre-installed)."
        )

    if not markdown.endswith("\n"):
        markdown += "\n"
    lines = markdown.splitlines(keepends=True)

    selected, total_lines, _start, end_idx, truncated = _apply_line_window(
        lines, offset, limit,
    )

    content = ""
    for i, line in enumerate(selected, start=offset):
        content += f"{i}|{line}"

    content_len = len(content)
    max_chars = get_max_bytes()
    if content_len > max_chars:
        return json.dumps({
            "error": (
                f"Read produced {content_len:,} characters which exceeds "
                f"the safety limit ({max_chars:,} chars). "
                "Use offset and limit to read a smaller range. "
                f"The document has {total_lines} lines total."
            ),
            "path": path,
            "total_lines": total_lines,
            "file_size": file_size,
        }, ensure_ascii=False)

    logger.info(
        "event=document.parse path=%s ext=%s bytes_md=%d total_lines=%d",
        path, resolved.suffix.lower(), len(markdown), total_lines,
    )

    return json.dumps({
        "content": content,
        "path": path,
        "total_lines": total_lines,
        "lines_shown": len(selected),
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "file_size": file_size,
    }, ensure_ascii=False)
```

- [ ] **Step 4: Run the full file_ops_documents suite**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/tools/builtin/file_ops.py tests/tools/test_file_ops_documents.py
git commit -m "feat(file_ops): _handle_document returns markdown via markitdown

Wires the document handler to the file-backed cache and parser.  PDFs,
docx, xlsx, and pptx now flow through read_file end-to-end with the
same offset/limit pagination as text files."
```

---

## Task 6: Update the `read_file` tool description

Load-bearing for agent behavior — without this the model still reflexively reaches for `pip install pypdf`.

**Files:**
- Modify: `surogates/tools/builtin/file_ops.py` — `READ_FILE_SCHEMA` description (~line 502-511)

- [ ] **Step 1: Write the description-check test**

Append to `tests/tools/test_file_ops_documents.py`:

```python
def test_read_file_description_mentions_native_document_handling() -> None:
    from surogates.tools.builtin.file_ops import READ_FILE_SCHEMA
    desc = READ_FILE_SCHEMA.description.lower()
    assert ".pdf" in desc
    assert ".docx" in desc
    assert "vision" in desc or "image" in desc
    # Negative guard: the old "Cannot read images or binary files" wording
    # is gone (or qualified), otherwise the agent will keep avoiding the tool.
    assert "cannot read images" not in desc
```

- [ ] **Step 2: Run to confirm it fails**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py::test_read_file_description_mentions_native_document_handling -v
```

Expected: FAIL (description still says "Cannot read images or binary files").

- [ ] **Step 3: Update the description**

In `surogates/tools/builtin/file_ops.py`, replace the `READ_FILE_SCHEMA.description` value with:

```python
        "Read a file with line numbers and pagination. Use this instead of "
        "cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. "
        "Handles plain text plus .pdf, .docx, .xlsx, .pptx (parsed to "
        "markdown) and images (described by a vision model) — do NOT "
        "pre-extract documents with subprocess tools. Suggests similar "
        "filenames if not found. Use offset and limit for large files. "
        "Reads exceeding ~100K characters are rejected; use offset and "
        "limit to read specific sections."
```

- [ ] **Step 4: Run all file_ops_documents tests**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_documents.py -v
```

Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/tools/builtin/file_ops.py tests/tools/test_file_ops_documents.py
git commit -m "feat(file_ops): advertise native document + image support in schema

The agent's tool selection is driven by the schema description; without
this the model still reflexively reaches for pip install pypdf when it
sees a .pdf path."
```

---

## Task 7: Worker-side image pre-dispatch branch

Adds the `read_file` → `vision_analyze` redirection in `tool_exec.py`. This must happen on the worker because the sandbox has no LLM client.

**Files:**
- Create: `surogates/harness/image_read.py`
- Modify: `surogates/harness/tool_exec.py` — insert branch before sandbox dispatch (~line 1214)
- Create: `tests/tools/test_file_ops_images_via_worker.py`

- [ ] **Step 1: Write the failing test**

Create `/work/surogates/tests/tools/test_file_ops_images_via_worker.py`:

```python
"""Unit tests for the worker's read_file→vision_analyze pre-dispatch branch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_read_png_calls_vision_and_renders_as_read_file(
    tmp_path: Path,
) -> None:
    from surogates.harness.image_read import handle_image_read

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n placeholder")

    async def fake_vision_dispatch(
        tool_name: str, args: dict, **kwargs,
    ) -> str:
        assert tool_name == "vision_analyze"
        assert args["image"] == str(img)
        return json.dumps({
            "analysis": "A bar chart with three columns.",
            "model": "gpt-x",
        })

    result_json = await handle_image_read(
        path=str(img),
        arguments={"path": str(img)},
        dispatch=fake_vision_dispatch,
        kwargs={"workspace_path": str(tmp_path)},
    )
    result = json.loads(result_json)
    assert "error" not in result, result
    assert "# Image: chart.png" in result["content"]
    assert "A bar chart with three columns." in result["content"]
    assert result["path"] == str(img)


@pytest.mark.asyncio
async def test_read_image_caches_analysis(tmp_path: Path) -> None:
    from surogates.harness.image_read import handle_image_read, image_cache

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG placeholder")

    image_cache().clear()
    calls = {"n": 0}

    async def counting_dispatch(tool_name: str, args: dict, **kwargs) -> str:
        calls["n"] += 1
        return json.dumps({"analysis": "described"})

    await handle_image_read(
        str(img), {"path": str(img)}, counting_dispatch, {},
    )
    await handle_image_read(
        str(img), {"path": str(img), "offset": 1, "limit": 100},
        counting_dispatch, {},
    )
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_read_image_no_vision_configured(tmp_path: Path) -> None:
    from surogates.harness.image_read import handle_image_read

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG placeholder")

    async def err_dispatch(tool_name: str, args: dict, **kwargs) -> str:
        return json.dumps({
            "error": "vision_analyze is not available: no vision LLM configured",
        })

    result_json = await handle_image_read(
        str(img), {"path": str(img)}, err_dispatch, {},
    )
    result = json.loads(result_json)
    assert "error" in result
    assert "vision" in result["error"].lower()
```

- [ ] **Step 2: Run to confirm it fails**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_images_via_worker.py -v
```

Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `surogates/harness/image_read.py`**

Create `/work/surogates/surogates/harness/image_read.py`:

```python
"""Worker-side `read_file` branch for image paths.

The K8s sandbox does not have LLM clients or the vision configuration
needed to call OpenAI / Anthropic, so the worker intercepts
``read_file(image.png)`` calls before they reach sandbox dispatch and
routes them through ``vision_analyze``.  The result is reshaped into a
``read_file``-style envelope so the LLM never sees ``vision_analyze``
output unless it called that tool explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ENTRIES = 8
_DEFAULT_MAX_ENTRY_BYTES = 2 * 1024 * 1024


class _ImageCache:
    """Tiny in-memory LRU.  Worker-process scoped."""

    def __init__(
        self,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_entry_bytes: int = _DEFAULT_MAX_ENTRY_BYTES,
    ) -> None:
        self._max_entries = max_entries
        self._max_entry_bytes = max_entry_bytes
        self._lock = threading.Lock()
        self._store: OrderedDict[tuple, str] = OrderedDict()

    def get(self, key: tuple) -> str | None:
        with self._lock:
            if key not in self._store:
                return None
            value = self._store.pop(key)
            self._store[key] = value
            return value

    def put(self, key: tuple, value: str) -> None:
        if len(value.encode("utf-8")) > self._max_entry_bytes:
            return
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_CACHE = _ImageCache()


def image_cache() -> _ImageCache:
    return _CACHE


async def _build_key(path: str, kwargs: dict[str, Any]) -> tuple | None:
    """Return the cache key for a local or storage-backed workspace."""
    workspace_path = kwargs.get("workspace_path")
    local_path = path
    if workspace_path and not os.path.isabs(path):
        local_path = os.path.join(str(workspace_path), path)

    if os.path.exists(local_path):
        try:
            st = os.stat(local_path)
            return ("local", os.path.realpath(local_path), st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    storage = kwargs.get("storage")
    session_id = kwargs.get("session_id")
    session_config = kwargs.get("session_config") or {}
    bucket = session_config.get("storage_bucket")
    if storage is not None and session_id is not None and bucket:
        from surogates.storage.tenant import prefixed_session_workspace_key

        workspace_key = path
        if workspace_path:
            try:
                from pathlib import PurePosixPath
                workspace_key = (
                    PurePosixPath(path)
                    .relative_to(PurePosixPath(str(workspace_path)))
                    .as_posix()
                )
            except ValueError:
                workspace_key = path
        workspace_key = workspace_key.lstrip("/")
        key = prefixed_session_workspace_key(session_config, session_id, workspace_key)
        try:
            stat = await storage.stat(bucket, key)
        except Exception:
            return None
        return (
            "storage",
            str(session_id),
            key,
            stat.get("size"),
            str(stat.get("modified")),
        )

    return None


async def handle_image_read(
    path: str,
    arguments: dict[str, Any],
    dispatch: Callable[..., Awaitable[str]],
    kwargs: dict[str, Any],
) -> str:
    """Run vision_analyze on the image and render the result as read_file.

    ``dispatch`` is the worker's ``tools.dispatch`` (or any callable with
    the same signature).  Tests inject a stub.
    """
    from surogates.tools.builtin.file_ops import (
        _apply_line_window,
        get_max_bytes,
        get_max_lines,
    )

    offset = max(arguments.get("offset", 1), 1)
    limit = min(arguments.get("limit", 500), get_max_lines())

    cache_disabled = os.environ.get("READ_IMAGE_CACHE_DISABLED") == "1"
    key = None if cache_disabled else await _build_key(path, kwargs)

    analysis: str | None = None
    cached = False
    if key is not None:
        analysis = _CACHE.get(key)
        cached = analysis is not None

    if analysis is None:
        raw = await dispatch("vision_analyze", {"image": path}, **kwargs)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({
                "error": f"vision_analyze returned non-JSON output: {raw[:200]}",
            }, ensure_ascii=False)
        if "error" in parsed:
            return json.dumps({
                "error": (
                    f"read_file could not analyze image '{path}': "
                    f"{parsed['error']}"
                ),
            }, ensure_ascii=False)
        analysis = parsed.get("analysis") or ""
        if key is not None and analysis:
            _CACHE.put(key, analysis)

    filename = Path(path).name
    markdown = f"# Image: {filename}\n\n{analysis}\n"
    lines = markdown.splitlines(keepends=True)

    selected, total_lines, _start, _end, truncated = _apply_line_window(
        lines, offset, limit,
    )

    content = ""
    for i, line in enumerate(selected, start=offset):
        content += f"{i}|{line}"

    content_len = len(content)
    max_chars = get_max_bytes()
    if content_len > max_chars:
        return json.dumps({
            "error": (
                f"Image analysis produced {content_len:,} characters which "
                f"exceeds the safety limit ({max_chars:,} chars). Use "
                "offset and limit to read a smaller range."
            ),
            "path": path,
        }, ensure_ascii=False)

    logger.info(
        "event=image.analyze path=%s bytes=%d cached=%s",
        path, len(analysis), cached,
    )

    return json.dumps({
        "content": content,
        "path": path,
        "total_lines": total_lines,
        "lines_shown": len(selected),
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
    }, ensure_ascii=False)
```

- [ ] **Step 4: Wire the branch into `tool_exec.py`**

In `/work/surogates/surogates/harness/tool_exec.py`, find the line `# Execute the tool, capturing errors as results (never crash the loop).` (~1197). After the existing `from surogates.tools.router import TOOL_LOCATIONS, ToolLocation` import and the `mcp__` short-circuit (~1209-1212), and before `if location == ToolLocation.SANDBOX and sandbox_pool is not None:` (~1214), insert:

```python
        # ── read_file image branch ─────────────────────────────────────
        # Images can't be analyzed inside the sandbox (no LLM client).
        # When read_file targets an image path, redirect to vision_analyze
        # in-process and reshape the result as a read_file envelope.
        if tool_name == "read_file":
            from surogates.tools.builtin.file_ops import IMAGE_EXTENSIONS
            image_path = (tool_args or {}).get("path") if isinstance(tool_args, dict) else None
            if image_path:
                _ext = os.path.splitext(image_path)[1].lower()
                if _ext in IMAGE_EXTENSIONS:
                    from surogates.harness.image_read import handle_image_read
                    result_content = await handle_image_read(
                        path=image_path,
                        arguments=tool_args,
                        dispatch=tools.dispatch,
                        kwargs={
                            "session_id": str(session.id),
                            "agent_id": session.agent_id,
                            "tenant": tenant,
                            "session_store": store,
                            "redis": redis,
                            "storage": storage,
                            "workspace_path": workspace_path,
                            "api_client": api_client,
                            "session_factory": session_factory,
                            "llm_client": llm_client,
                            "model": model or getattr(session, "model", None),
                            "vision_llm_client": vision_llm_client,
                            "vision_model": vision_model,
                            "tools": tools,
                            "tool_call_id": tool_call_id,
                            "session_config": session.config,
                        },
                    )
                    # Skip the normal sandbox/harness dispatch — we
                    # already produced the result.
                    location = None  # sentinel: result is ready
```

Then, immediately after this block, guard the existing dispatch so it does not run when we already produced a result:

Find the existing `if location == ToolLocation.SANDBOX and sandbox_pool is not None:` and wrap the whole if/else with an `if location is not None:` outer check. Specifically, replace:

```python
        if location == ToolLocation.SANDBOX and sandbox_pool is not None:
```

with:

```python
        if location is None:
            pass  # result_content already set by the image branch
        elif location == ToolLocation.SANDBOX and sandbox_pool is not None:
```

Also add `import os` at the top of the file if it isn't already imported (a quick `grep "^import os" tool_exec.py` will tell you).

- [ ] **Step 5: Run the image branch tests**

```bash
cd /work/surogates && uv run pytest tests/tools/test_file_ops_images_via_worker.py -v
```

Expected: All three PASS. These tests exercise `handle_image_read` in
isolation; add a focused `execute_single_tool` regression if the branch wiring
changes during implementation, because handler-only tests will not catch a
misplaced pre-dispatch branch.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/harness/image_read.py surogates/harness/tool_exec.py tests/tools/test_file_ops_images_via_worker.py
git commit -m "feat(harness): route read_file(image) through vision_analyze

Image analysis requires LLM clients the sandbox doesn't have, so the
worker intercepts read_file on image paths before sandbox dispatch and
shapes the vision_analyze output as a read_file result.  Worker-local
in-memory LRU caches the analysis."
```

---

## Task 8: Integration test — real documents through an in-process workspace

This is the hermetic integration test for the document parser path: a real
workspace file exists under `workspace_path`, `read_file(path)` is called, and
the tool returns markdown. It intentionally does not exercise the HTTP
workspace upload route; add a separate API-route test only if upload handling
changes as part of the implementation.

**Files:**
- Create: `tests/integration/test_read_document_e2e.py`

- [ ] **Step 1: Write the failing integration test**

The test below does not need session/upload boilerplate because document
parsing in `read_file` only requires the file to exist on disk under
`workspace_path`. We use `tmp_path` as the workspace directly.

Create `/work/surogates/tests/integration/test_read_document_e2e.py`:

```python
"""End-to-end: real workspace documents read as markdown via read_file."""

from __future__ import annotations

import json
import pathlib

import pytest

from tests.tools.fixtures.build_documents import (
    build_minimal_docx,
    build_minimal_pdf,
    build_minimal_xlsx,
    build_minimal_pptx,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "builder,filename,probe",
    [
        (build_minimal_pdf, "doc.pdf", "Hello PDF"),
        (build_minimal_docx, "doc.docx", "Hello DOCX"),
        (build_minimal_xlsx, "doc.xlsx", "Alpha"),
        (build_minimal_pptx, "doc.pptx", "Hello PPTX"),
    ],
)
async def test_read_file_returns_markdown_for_each_format(
    tmp_path: pathlib.Path,
    builder,
    filename: str,
    probe: str,
) -> None:
    """Each office format must round-trip via read_file with no extra setup.

    Uses the in-process workspace (no S3) so the test stays hermetic.
    """
    from surogates.tools.builtin.file_ops import _read_file_handler

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
```

- [ ] **Step 2: Run to confirm it passes (after Task 5)**

```bash
cd /work/surogates && uv run pytest tests/integration/test_read_document_e2e.py -v
```

Expected: 4/4 PASS.

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add tests/integration/test_read_document_e2e.py
git commit -m "test(integration): real-document round trip via read_file"
```

---

## Task 9: Verify dependency lock and parser availability

Task 3 already added `markitdown[pptx]` and `reportlab` before parser tests
could run. This task is a verification gate that catches a stale `uv.lock` or
an environment where the parser packages are missing.

**Files:**
- Verify: `pyproject.toml`
- Verify: `uv.lock`

- [ ] **Step 1: Inspect the current dependency block and lockfile**

```bash
cd /work/surogates && /bin/grep -n "markitdown\|reportlab\|dependencies\|dependency-groups" pyproject.toml uv.lock | head -40
```

- [ ] **Step 2: Re-sync and run the document tests**

```bash
cd /work/surogates && uv sync && uv run pytest tests/tools/ tests/integration/test_read_document_e2e.py -v
```

Expected: All tests PASS.

- [ ] **Step 3: Commit only if `uv sync` changed the lockfile**

```bash
cd /work/surogates
git add pyproject.toml uv.lock
git commit -m "build(deps): refresh document parser dependency lock"
```

---

## Task 10: Update `CLAUDE.md`

Two-line documentation tweak so agents reading the project guide know the new behavior.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Locate the section that mentions `read_file` or file reading**

```bash
cd /work/surogates && /bin/grep -n "read_file\|read a file\|reading files" CLAUDE.md | head -10
```

- [ ] **Step 2: Add a note near the most relevant section**

Insert this paragraph in the file-tools section of `/work/surogates/CLAUDE.md` (wherever `read_file` is discussed):

```markdown
`read_file` now natively parses `.pdf`, `.docx`, `.xlsx`, `.pptx` (via
markitdown, returning markdown) and image files (via the worker's vision
model, returning a description). Do NOT pre-extract these formats with
subprocess tools — call `read_file(path)` directly.
```

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add CLAUDE.md
git commit -m "docs(claude.md): note read_file native document/image support"
```

---

## Task 11: Smoke test the whole stack and verify

Before merging, run all the new tests and a wider regression sweep to make sure nothing else broke. No new code; this is a verification gate.

- [ ] **Step 1: Run the new tests**

```bash
cd /work/surogates && uv run pytest tests/tools/ tests/integration/test_read_document_e2e.py -v
```

Expected: All PASS.

- [ ] **Step 2: Run the existing tool tests that we touched the surface of**

```bash
cd /work/surogates && uv run pytest tests/test_file_ops_tracker.py tests/ -k "file_ops or tool_exec" -v
```

Expected: All PASS. If anything fails, root-cause before continuing — the refactor in Task 1 was supposed to be mechanical.

- [ ] **Step 3: Lint / typecheck the changed files**

```bash
cd /work/surogates && uv run ruff check surogates/tools/builtin/file_ops.py surogates/tools/utils/document_cache.py surogates/tools/utils/binary_extensions.py surogates/harness/tool_exec.py surogates/harness/image_read.py
```

Expected: No errors. (If the project uses mypy or another type checker, run it too.)

- [ ] **Step 4: Confirm the sandbox image rebuild is captured in the commit log**

```bash
cd /work/surogates && git log --oneline -n 20
```

Verify that at least one commit message references the need to rebuild
`ghcr.io/invergent-ai/surogates-agent-sandbox`. If not, amend the latest
commit body or add a release note.

- [ ] **Step 5: Final commit (only if something needed touch-up)**

Skip this step if no changes were needed. Otherwise:

```bash
cd /work/surogates
git add -A
git commit -m "chore: address smoke-test findings"
```

---

## Notes for the implementer

- **The refactor in Task 1 is mechanical.** If the snapshot test in Step 2 of Task 1 breaks after the refactor, the lift wasn't faithful — re-read the original `_read_file_handler` body and copy more carefully. Do not "fix forward" by adjusting the test.
- **`asyncio.to_thread` requires Python 3.9+.** This repo targets 3.12, so no compatibility worry.
- **`fcntl.flock` is POSIX-only.** The sandbox runs on Linux; that's fine. If anyone runs this on Windows (unlikely for the worker, impossible for the sandbox), the `OSError` catch in `_maybe_store` will just disable write caching — safe degradation.
- **`READ_IMAGE_CACHE_DISABLED=1`** turns off the worker image cache. Use this if vision-LLM costs spike unexpectedly in production.
- **Production deployment requires rebuilding the sandbox image.** The harness ships, but document parsing happens inside the sandbox where the running image is whatever's been built. Make sure your release pipeline rebuilds `ghcr.io/invergent-ai/surogates-agent-sandbox` with the new code before claiming this is shipped.
