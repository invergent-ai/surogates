# Inline Small Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a user sends a message with a small (<2 MB) document or text-file attachment, the harness parses it server-side and embeds the rendered content into the user message before the LLM sees it — eliminating the `read_file` round-trip for the common case.

**Architecture:** Two surface changes. (1) In the send-message route, after attachment validation, attempt inline parsing per file and store the result on the `AttachmentRef`. (2) In `_rebuild_messages`, when emitting the user message into the LLM history, append a fenced block per attachment that has `inlined_text` set. The system attachments note ignores inlined entries; for skip cases it annotates the reason.

**Tech Stack:** Python 3.12, pytest, Pydantic v2, `pymupdf4llm`/`markitdown` (already added by the previous PR), `surogates.tools.utils.document_cache` (already in the codebase).

**Spec:** [2026-05-25-inline-attachments-design.md](../specs/2026-05-25-inline-attachments-design.md)

---

## Task tracker

Updated before every commit.

- [x] Task 1 — Extend `AttachmentRef` with `inlined_text`, `inlined_render_kind`, `inline_skip_reason`
- [x] Task 2 — `_inline_extension_kind` helper
- [x] Task 3 — `_materialize_for_cache` helper (S3 → deterministic temp source path)
- [x] Task 4 — `_try_inline_attachment` helper
- [x] Task 5 — Wire the helpers into the send-message route
- [x] Task 6 — `_render_inlined_attachments` renderer
- [x] Task 7 — Call the renderer in `_rebuild_messages`
- [x] Task 8 — Revise `_attachments_note` (skip inlined, annotate skip-reason)
- [x] Task 9 — Integration test for end-to-end round trip
- [x] Task 10 — History-replay regression test
- [x] Task 11 — Update `docs/tools/index.md`
- [ ] **Task 12 (in progress)** — Smoke test and verify

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| [surogates/api/routes/sessions.py](../../../surogates/api/routes/sessions.py) | `AttachmentRef` extension, inline helpers (`_inline_extension_kind`, `_materialize_for_cache`, `_try_inline_attachment`, `_INLINE_*` constants), wire-up in the send-message route | Modify |
| [surogates/harness/loop.py](../../../surogates/harness/loop.py) | `_render_inlined_attachments` renderer, `_rebuild_messages` user-message branch, revised `_attachments_note` | Modify |
| [tests/api/__init__.py](../../../tests/api/__init__.py) | Package marker (likely already exists; create if missing) | Create or no-op |
| [tests/api/test_attachment_inlining.py](../../../tests/api/test_attachment_inlining.py) | Unit tests for `_try_inline_attachment` and helpers | Create |
| [tests/harness/__init__.py](../../../tests/harness/__init__.py) | Package marker (create if missing) | Create or no-op |
| [tests/harness/test_attachment_rendering.py](../../../tests/harness/test_attachment_rendering.py) | Renderer + revised attachments-note unit tests | Create |
| [tests/integration/test_inline_attachments_e2e.py](../../../tests/integration/test_inline_attachments_e2e.py) | End-to-end round-trip via `_rebuild_messages` | Create |
| [tests/integration/test_attachment_history_replay.py](../../../tests/integration/test_attachment_history_replay.py) | Persist + replay determinism | Create |
| [docs/tools/index.md](../../tools/index.md) | Note that small attachments are inlined automatically | Modify (1-2 lines) |

---

## Task 1: Extend `AttachmentRef` with inline fields

Add three optional server-set fields. Add a request-side validator that strips any client-supplied values so a hostile client cannot inject content or spoof a skip-reason.

**Files:**
- Modify: [surogates/api/routes/sessions.py:88-127](../../../surogates/api/routes/sessions.py) (`AttachmentRef` class)
- Modify: [surogates/api/routes/sessions.py:130-187](../../../surogates/api/routes/sessions.py) (`SendMessageRequest` — add the stripping validator)
- Create: `tests/api/__init__.py` (empty marker if not present)
- Create: `tests/api/test_attachment_inlining.py`

- [ ] **Step 1: Create the test package marker**

```bash
mkdir -p /work/surogates/tests/api
test -f /work/surogates/tests/api/__init__.py || touch /work/surogates/tests/api/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `/work/surogates/tests/api/test_attachment_inlining.py`:

```python
"""Unit tests for inline-attachment parsing in the send-message route."""

from __future__ import annotations

import pytest


def test_attachment_ref_accepts_new_inline_fields() -> None:
    from surogates.api.routes.sessions import AttachmentRef

    ref = AttachmentRef(
        path="uploads/x.pdf",
        filename="x.pdf",
        inlined_text="# heading\nbody",
        inlined_render_kind="markdown",
        inline_skip_reason=None,
    )
    assert ref.inlined_text == "# heading\nbody"
    assert ref.inlined_render_kind == "markdown"
    assert ref.inline_skip_reason is None


def test_send_message_request_strips_client_supplied_inline_fields() -> None:
    """A hostile client cannot inject inlined_text into its own user message."""
    from surogates.api.routes.sessions import SendMessageRequest

    req = SendMessageRequest.model_validate({
        "content": "hi",
        "attachments": [
            {
                "path": "uploads/x.pdf",
                "filename": "x.pdf",
                "inlined_text": "INJECTED",
                "inlined_render_kind": "markdown",
                "inline_skip_reason": "parse_error",
            }
        ],
    })
    assert req.attachments is not None
    assert req.attachments[0].inlined_text is None
    assert req.attachments[0].inlined_render_kind is None
    assert req.attachments[0].inline_skip_reason is None
```

- [ ] **Step 3: Run the test (expect FAIL — fields don't exist yet)**

```bash
cd /work/surogates && uv run pytest tests/api/test_attachment_inlining.py -v
```

Expected: both fail with `ValidationError` (unknown field) or `AttributeError`.

- [ ] **Step 4: Add the three fields to `AttachmentRef`**

In `/work/surogates/surogates/api/routes/sessions.py`, replace the `AttachmentRef` class body (around line 88-127) so the class reads:

```python
class AttachmentRef(BaseModel):
    """A reference to a file previously uploaded to the session workspace.

    The harness validates that ``path`` resolves to an existing object in the
    session's workspace bucket before persisting it on the user.message event.
    ``size`` is a client-provided hint; the harness overwrites it with the
    real storage size during validation.

    ``inlined_text``, ``inlined_render_kind``, and ``inline_skip_reason``
    are server-set: the send-message route attempts to parse small
    documents (<2 MB) and embed the result so the LLM sees the content
    directly without calling ``read_file``.  Clients must not set these
    fields -- :class:`SendMessageRequest` strips them defensively before
    they reach this model.
    """

    path: str
    filename: str
    mime_type: str | None = None
    size: int | None = None
    inlined_text: str | None = None
    inlined_render_kind: Literal["markdown", "text"] | None = None
    inline_skip_reason: Literal[
        "parse_error",
        "parse_timeout",
        "decode_error",
        "oversize_output",
        "empty_output",
    ] | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v:
            raise ValueError("attachment path must be non-empty")
        if v.startswith("/"):
            raise ValueError("attachment path must be workspace-relative")
        if "\x00" in v:
            raise ValueError("attachment path must not contain NUL")
        parts = v.split("/")
        if any(part == ".." for part in parts):
            raise ValueError("attachment path must not contain '..' segments")
        return v

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, v: str) -> str:
        if not v:
            raise ValueError("attachment filename must be non-empty")
        if "/" in v or "\\" in v:
            raise ValueError(
                "attachment filename must not contain path separators",
            )
        if "\x00" in v:
            raise ValueError("attachment filename must not contain NUL")
        return v
```

You will also need to add `Literal` to the imports at the top of the file if it is not already present. Run:

```bash
cd /work/surogates && /bin/grep "from typing import" surogates/api/routes/sessions.py | head -3
```

If `Literal` is not in the import, add it. Otherwise, no change.

- [ ] **Step 5: Add the client-strip validator to `SendMessageRequest`**

In the same file, add a `model_validator(mode="before")` to `SendMessageRequest` that scrubs the three server-set fields off each attachment dict. Insert just before the existing `@field_validator("images")` (around line 148):

```python
    @model_validator(mode="before")
    @classmethod
    def _strip_server_set_attachment_fields(cls, values: Any) -> Any:
        """Drop any server-set inline fields a client tried to spoof."""
        if not isinstance(values, dict):
            return values
        atts = values.get("attachments")
        if not isinstance(atts, list):
            return values
        for item in atts:
            if isinstance(item, dict):
                item.pop("inlined_text", None)
                item.pop("inlined_render_kind", None)
                item.pop("inline_skip_reason", None)
        return values
```

You may need to add `model_validator` to the Pydantic imports at the top of the file. Run:

```bash
cd /work/surogates && /bin/grep "from pydantic" surogates/api/routes/sessions.py | head -3
```

If `model_validator` is missing, add it to the import.

- [ ] **Step 6: Run the tests (expect PASS)**

```bash
cd /work/surogates && uv run pytest tests/api/test_attachment_inlining.py -v
```

Expected: both PASS.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/api/routes/sessions.py tests/api/__init__.py tests/api/test_attachment_inlining.py
git commit -m "feat(api): add inline fields to AttachmentRef

inlined_text, inlined_render_kind, and inline_skip_reason are server-
set on the AttachmentRef; SendMessageRequest's pre-validator scrubs
them off any client payload so a hostile caller cannot inject content
or spoof a skip-reason on its own user message."
```

---

## Task 2: `_inline_extension_kind` helper

Pure-function dispatcher mapping a filename to `"document"`, `"text"`, or `None`.

**Files:**
- Modify: `surogates/api/routes/sessions.py` (add the helper near the other module-level constants around line 71)
- Modify: `tests/api/test_attachment_inlining.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/api/test_attachment_inlining.py`:

```python
@pytest.mark.parametrize(
    "filename, expected",
    [
        ("report.pdf", "document"),
        ("contract.docx", "document"),
        ("model.xlsx", "document"),
        ("deck.pptx", "document"),
        ("notes.txt", "text"),
        ("README.md", "text"),
        ("data.json", "text"),
        ("rows.csv", "text"),
        ("rows.tsv", "text"),
        ("config.yaml", "text"),
        ("config.yml", "text"),
        ("server.log", "text"),
        ("uppercase.PDF", "document"),  # case-insensitive
        ("UPPERCASE.MD", "text"),
        ("img.png", None),
        ("bundle.zip", None),
        ("noext", None),
        ("trailing-dot.", None),
    ],
)
def test_inline_extension_kind(filename: str, expected: str | None) -> None:
    from surogates.api.routes.sessions import _inline_extension_kind

    assert _inline_extension_kind(filename) == expected
```

- [ ] **Step 2: Run the tests (expect ImportError / AttributeError)**

```bash
cd /work/surogates && uv run pytest tests/api/test_attachment_inlining.py::test_inline_extension_kind -v
```

Expected: fail with `ImportError` because the helper doesn't exist.

- [ ] **Step 3: Implement the helper**

In `/work/surogates/surogates/api/routes/sessions.py`, add after the `_MAX_ATTACHMENTS_TOTAL_BYTES` constant (around line 71):

```python
_INLINE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB raw cap for inline parsing
_INLINE_RENDERED_CAP_CHARS = 200_000  # 200 KB of rendered text/markdown

_INLINE_DOC_EXTS = frozenset({".pdf", ".docx", ".xlsx", ".pptx"})
_INLINE_TEXT_EXTS = frozenset({
    ".txt", ".md", ".json", ".csv", ".tsv",
    ".yaml", ".yml", ".log",
})


def _inline_extension_kind(filename: str) -> Literal["document", "text"] | None:
    """Map a filename to its inline-parsing kind, or None if unsupported."""
    import os.path as _ospath  # noqa: PLC0415

    ext = _ospath.splitext(filename)[1].lower()
    if not ext:
        return None
    if ext in _INLINE_DOC_EXTS:
        return "document"
    if ext in _INLINE_TEXT_EXTS:
        return "text"
    return None
```

(The local `os.path` import keeps the top of the file from gaining yet another global import for one use site.)

- [ ] **Step 4: Run the tests (expect PASS)**

```bash
cd /work/surogates && uv run pytest tests/api/test_attachment_inlining.py::test_inline_extension_kind -v
```

Expected: all parametrised cases PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/api/routes/sessions.py tests/api/test_attachment_inlining.py
git commit -m "feat(api): _inline_extension_kind dispatcher

Pure-function extension-to-kind mapper used by the send-message route
to decide whether an attachment is eligible for inline parsing."
```

---

## Task 3: `_materialize_for_cache` helper

The document cache is keyed on `(absolute_path, mtime_ns, size, ext)`. For workspace attachments held in S3/R2, we materialize the bytes to a deterministic temp path so that re-sending the same `(bucket, storage_key, size, modified)` reuses the same source file and therefore the cached parse instead of triggering a fresh `pymupdf4llm.to_markdown` call.

**Files:**
- Modify: `surogates/api/routes/sessions.py` — new helper
- Modify: `tests/api/test_attachment_inlining.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/api/test_attachment_inlining.py`:

```python
from pathlib import Path


def test_materialize_for_cache_writes_and_returns_deterministic_path(
    tmp_path,
) -> None:
    from surogates.api.routes.sessions import _materialize_for_cache

    raw = b"hello pdf bytes"
    path1 = _materialize_for_cache(
        raw_bytes=raw,
        bucket="b1",
        storage_key="proj/agent/sess/uploads/file.pdf",
        size=len(raw),
        modified="2026-05-25T05:00:00Z",
        suffix=".pdf",
        cache_root=tmp_path,
    )
    assert path1.exists()
    assert path1.read_bytes() == raw
    assert path1.suffix == ".pdf"

    # Same key → same path
    path2 = _materialize_for_cache(
        raw_bytes=raw,
        bucket="b1",
        storage_key="proj/agent/sess/uploads/file.pdf",
        size=len(raw),
        modified="2026-05-25T05:00:00Z",
        suffix=".pdf",
        cache_root=tmp_path,
    )
    assert path2 == path1


def test_materialize_for_cache_different_modified_produces_different_path(
    tmp_path,
) -> None:
    from surogates.api.routes.sessions import _materialize_for_cache

    raw = b"hello"
    a = _materialize_for_cache(
        raw_bytes=raw, bucket="b", storage_key="k",
        size=5, modified="t1", suffix=".txt", cache_root=tmp_path,
    )
    b = _materialize_for_cache(
        raw_bytes=raw, bucket="b", storage_key="k",
        size=5, modified="t2", suffix=".txt", cache_root=tmp_path,
    )
    assert a != b
```

- [ ] **Step 2: Run (expect ImportError)**

```bash
cd /work/surogates && uv run pytest tests/api/test_attachment_inlining.py -v -k "materialize"
```

Expected: ImportError.

- [ ] **Step 3: Implement the helper**

In `/work/surogates/surogates/api/routes/sessions.py`, add:

```python
_INLINE_MATERIALIZE_ROOT = Path("/tmp/surogates-attachment-inline")


def _materialize_for_cache(
    raw_bytes: bytes,
    *,
    bucket: str,
    storage_key: str,
    size: int,
    modified: str,
    suffix: str,
    cache_root: Path = _INLINE_MATERIALIZE_ROOT,
) -> Path:
    """Write ``raw_bytes`` to a deterministic temp file keyed on identity.

    The document cache hashes the source path's ``(absolute_path,
    mtime_ns, size, ext)`` tuple.  By materialising the bytes once into
    a deterministic location, re-sending the same attachment within a
    pod's lifetime hits the cache instead of re-parsing.

    The filename embeds a SHA-256 of (bucket, storage_key, size,
    modified) so distinct uploads never collide and re-uploads with
    different bytes get a fresh entry.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(
        f"{bucket}|{storage_key}|{size}|{modified}".encode("utf-8"),
    ).hexdigest()
    target = cache_root / f"{fingerprint}{suffix.lower()}"
    if not target.exists():
        tmp_file = tempfile.NamedTemporaryFile(
            dir=cache_root,
            prefix=f"{fingerprint}.",
            suffix=".part",
            delete=False,
        )
        tmp = Path(tmp_file.name)
        try:
            with tmp_file:
                tmp_file.write(raw_bytes)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
    return target
```

You will need `hashlib`, `os`, `tempfile`, and `Path` at the top of the file. Verify with:

```bash
cd /work/surogates && rg -n "^import (hashlib|os|tempfile)|^from pathlib" surogates/api/routes/sessions.py
```

Add any missing imports.

- [ ] **Step 4: Run the tests (expect PASS)**

```bash
cd /work/surogates && uv run pytest tests/api/test_attachment_inlining.py -v -k "materialize"
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/api/routes/sessions.py tests/api/test_attachment_inlining.py
git commit -m "feat(api): _materialize_for_cache deterministic temp path

Object-storage attachments need a stable on-disk path for the document
cache to recognise re-sends.  Materialise each attachment once per
(bucket, storage_key, size, modified) tuple under
/tmp/surogates-attachment-inline so the cache hits across messages."
```

---

## Task 4: `_try_inline_attachment` helper

Centralises the inline decision: cap check → kind dispatch → parse/decode → secondary cap → return `(text, kind, skip_reason)`.

**Files:**
- Modify: `surogates/api/routes/sessions.py`
- Modify: `tests/api/test_attachment_inlining.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/api/test_attachment_inlining.py`:

```python
@pytest.mark.asyncio
async def test_try_inline_attachment_pdf_returns_markdown(tmp_path) -> None:
    from surogates.api.routes.sessions import (
        AttachmentRef,
        _try_inline_attachment,
    )
    from tests.tools.fixtures.build_documents import build_minimal_pdf

    pdf = build_minimal_pdf(tmp_path / "p.pdf", heading="Hello PDF")
    attachment = AttachmentRef(
        path="uploads/p.pdf", filename="p.pdf", size=pdf.stat().st_size,
    )
    text, kind, reason = await _try_inline_attachment(
        attachment, pdf.read_bytes(), pdf,
    )
    assert kind == "markdown"
    assert reason is None
    assert "Hello PDF" in (text or "")


@pytest.mark.asyncio
async def test_try_inline_attachment_text_returns_text(tmp_path) -> None:
    from surogates.api.routes.sessions import (
        AttachmentRef,
        _try_inline_attachment,
    )

    src = tmp_path / "notes.md"
    raw = "# title\nhello world\n".encode("utf-8")
    src.write_bytes(raw)
    attachment = AttachmentRef(
        path="uploads/notes.md", filename="notes.md", size=len(raw),
    )
    text, kind, reason = await _try_inline_attachment(attachment, raw, src)
    assert kind == "text"
    assert reason is None
    assert text == "# title\nhello world\n"


@pytest.mark.asyncio
async def test_try_inline_attachment_skips_oversize_raw_bytes(tmp_path) -> None:
    from surogates.api.routes.sessions import (
        AttachmentRef,
        _INLINE_MAX_BYTES,
        _try_inline_attachment,
    )

    attachment = AttachmentRef(
        path="uploads/big.pdf", filename="big.pdf",
        size=_INLINE_MAX_BYTES + 1,
    )
    text, kind, reason = await _try_inline_attachment(
        attachment, b"", tmp_path / "big.pdf",
    )
    assert (text, kind, reason) == (None, None, None)


@pytest.mark.asyncio
async def test_try_inline_attachment_skips_unsupported_extension(
    tmp_path,
) -> None:
    from surogates.api.routes.sessions import (
        AttachmentRef,
        _try_inline_attachment,
    )

    attachment = AttachmentRef(
        path="uploads/x.bin", filename="x.bin", size=10,
    )
    text, kind, reason = await _try_inline_attachment(
        attachment, b"\x00\x01\x02\x03", tmp_path / "x.bin",
    )
    assert (text, kind, reason) == (None, None, None)


@pytest.mark.asyncio
async def test_try_inline_attachment_skips_on_decode_error(tmp_path) -> None:
    from surogates.api.routes.sessions import (
        AttachmentRef,
        _try_inline_attachment,
    )

    src = tmp_path / "garbled.txt"
    raw = b"\xff\xfe\x00invalid utf"
    src.write_bytes(raw)
    attachment = AttachmentRef(
        path="uploads/garbled.txt", filename="garbled.txt", size=len(raw),
    )
    text, kind, reason = await _try_inline_attachment(attachment, raw, src)
    assert text is None
    assert kind is None
    assert reason == "decode_error"


@pytest.mark.asyncio
async def test_try_inline_attachment_skips_when_parse_fails(
    tmp_path, monkeypatch,
) -> None:
    from surogates.api.routes.sessions import (
        AttachmentRef,
        _try_inline_attachment,
    )
    from surogates.tools.builtin import file_ops

    class RaisingPyMuPDF4LLM:
        def to_markdown(self, *args, **kwargs):
            raise RuntimeError("not a pdf")

    monkeypatch.setattr(
        file_ops, "_load_pymupdf4llm", lambda: RaisingPyMuPDF4LLM(),
    )

    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 placeholder")
    attachment = AttachmentRef(
        path="uploads/bad.pdf", filename="bad.pdf", size=bad.stat().st_size,
    )
    text, kind, reason = await _try_inline_attachment(
        attachment, bad.read_bytes(), bad,
    )
    assert text is None
    assert kind is None
    assert reason == "parse_error"


@pytest.mark.asyncio
async def test_try_inline_attachment_skips_when_markdown_empty(
    tmp_path, monkeypatch,
) -> None:
    from surogates.api.routes.sessions import (
        AttachmentRef,
        _try_inline_attachment,
    )
    from surogates.tools.builtin import file_ops

    class EmptyPyMuPDF4LLM:
        def to_markdown(self, *args, **kwargs):
            return "   \n  "

    monkeypatch.setattr(
        file_ops, "_load_pymupdf4llm", lambda: EmptyPyMuPDF4LLM(),
    )

    src = tmp_path / "empty.pdf"
    src.write_bytes(b"%PDF-1.4 placeholder")
    attachment = AttachmentRef(
        path="uploads/empty.pdf", filename="empty.pdf", size=src.stat().st_size,
    )
    text, kind, reason = await _try_inline_attachment(
        attachment, src.read_bytes(), src,
    )
    assert reason == "empty_output"


@pytest.mark.asyncio
async def test_try_inline_attachment_skips_when_oversize_output(
    tmp_path, monkeypatch,
) -> None:
    from surogates.api.routes.sessions import (
        AttachmentRef,
        _INLINE_RENDERED_CAP_CHARS,
        _try_inline_attachment,
    )

    src = tmp_path / "big.txt"
    raw = ("x" * (_INLINE_RENDERED_CAP_CHARS + 1)).encode("utf-8")
    src.write_bytes(raw)
    attachment = AttachmentRef(
        path="uploads/big.txt", filename="big.txt", size=len(raw),
    )
    text, kind, reason = await _try_inline_attachment(attachment, raw, src)
    assert text is None
    assert kind is None
    assert reason == "oversize_output"
```

- [ ] **Step 2: Run the tests (expect failure on every case until the helper exists)**

```bash
cd /work/surogates && uv run pytest tests/api/test_attachment_inlining.py -v -k "_try_inline_attachment"
```

Expected: ImportError on every case.

- [ ] **Step 3: Implement the helper**

In `/work/surogates/surogates/api/routes/sessions.py`, add (near the other inline helpers from Tasks 2-3):

```python
async def _try_inline_attachment(
    attachment: AttachmentRef,
    raw_bytes: bytes,
    document_path: Path | None,
) -> tuple[str | None, str | None, str | None]:
    """Decide whether to inline ``attachment`` and return the result.

    Returns ``(inlined_text, inlined_render_kind, inline_skip_reason)``.
    The first two are populated on success; the third is populated when
    a *supported* attachment was considered but skipped, so the prompt
    note can explain the fallback to the agent.  All three are ``None``
    when the file is silently out of scope (over the raw cap or
    unsupported extension) -- there is nothing useful to tell the LLM.
    """
    if attachment.size is not None and attachment.size > _INLINE_MAX_BYTES:
        return None, None, None
    kind = _inline_extension_kind(attachment.filename)
    if kind is None:
        return None, None, None

    if kind == "document":
        if document_path is None:
            return None, None, "parse_error"
        from surogates.tools.builtin.file_ops import (  # noqa: PLC0415
            DocumentParseError,
            _parse_document_to_markdown,
        )
        from surogates.tools.utils.document_cache import (  # noqa: PLC0415
            default_cache,
        )

        try:
            md = await default_cache().get_or_parse(
                document_path, _parse_document_to_markdown,
            )
        except DocumentParseError as exc:
            reason = (
                "parse_timeout"
                if "timeout" in exc.reason.lower()
                else "parse_error"
            )
            logger.info(
                "event=attachment.inline result=skip reason=%s "
                "filename=%s err=%s",
                reason, attachment.filename, exc.reason,
            )
            return None, None, reason
        if not md.strip():
            return None, None, "empty_output"
        if len(md) > _INLINE_RENDERED_CAP_CHARS:
            logger.info(
                "event=attachment.inline result=skip reason=oversize_output "
                "filename=%s chars=%d",
                attachment.filename, len(md),
            )
            return None, None, "oversize_output"
        return md, "markdown", None

    # kind == "text"
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "decode_error"
    if len(text) > _INLINE_RENDERED_CAP_CHARS:
        return None, None, "oversize_output"
    return text, "text", None
```

- [ ] **Step 4: Run the tests (expect all PASS)**

```bash
cd /work/surogates && uv run pytest tests/api/test_attachment_inlining.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/api/routes/sessions.py tests/api/test_attachment_inlining.py
git commit -m "feat(api): _try_inline_attachment per-file decision

Centralises the inline decision: raw-byte cap, extension kind dispatch,
parse via the document cache (so re-sends share work with read_file),
empty- and oversize-output guards, decode-error guard for text files.
Returns (inlined_text, render_kind, skip_reason) so the route handler
can persist the skip reason for the renderer to surface."
```

---

## Task 5: Wire the helpers into the send-message route

After the existing storage-key resolution + size check loop, call `_try_inline_attachment` per file and include the inline fields on the resolved payload.

**Files:**
- Modify: [surogates/api/routes/sessions.py:486-561](../../../surogates/api/routes/sessions.py) (attachment resolution loop)
- Test: covered transitively by the integration test in Task 9 (unit-testing the route requires the full FastAPI app)

- [ ] **Step 1: Read the current loop**

```bash
cd /work/surogates && /bin/sed -n '486,561p' surogates/api/routes/sessions.py
```

Confirm the structure matches what's in the spec.

- [ ] **Step 2: Replace the resolution loop**

In `/work/surogates/surogates/api/routes/sessions.py`, replace the section currently bounded by:

```
resolved: list[dict] = []
total_bytes = 0
for attachment in body.attachments:
    storage_key = prefixed_session_workspace_key(...)
    ...
    resolved.append({
        "path": attachment.path,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size": real_size,
    })
attachments_payload = resolved
```

with the version below.  The new logic:

1. Keeps every existing validation step (path resolution, exists, stat, per-file size cap, total cap).
2. After validation, fetches the bytes from storage (only when the file is small enough to be a candidate for inline). A `KeyError` at this point is still surfaced to the client as a broken attachment reference; other storage failures are allowed to propagate as 500s.
3. Uses the resolved local workspace file as the document-cache source when the storage backend exposes one; otherwise materialises document attachments into a deterministic temp path. Text attachments decode directly from `raw_bytes`.
4. Calls `_try_inline_attachment`.
5. Adds the three new fields to the resolved payload when set.

```python
        resolved: list[dict] = []
        total_bytes = 0
        for attachment in body.attachments:
            storage_key = prefixed_session_workspace_key(
                session.config, root_id, attachment.path,
            )
            if not await storage.exists(bucket, storage_key):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "Attachment path not found in workspace: "
                        f"{attachment.path}"
                    ),
                )
            try:
                stat = await storage.stat(bucket, storage_key)
            except KeyError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "Attachment path not found in workspace: "
                        f"{attachment.path}"
                    ),
                )
            real_size = int(stat.get("size", 0))
            if real_size > _MAX_ATTACHMENT_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Attachment exceeds "
                        f"{_MAX_ATTACHMENT_BYTES // 1_000_000}MB limit: "
                        f"{attachment.path} ({real_size} bytes)"
                    ),
                )
            total_bytes += real_size
            if total_bytes > _MAX_ATTACHMENTS_TOTAL_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "Attachments exceed total "
                        f"{_MAX_ATTACHMENTS_TOTAL_BYTES // 1_000_000}MB"
                        " limit per message"
                    ),
                )

            # ── Inline-attachment branch ─────────────────────────────
            # For files small enough and of a supported type, parse or
            # decode the content server-side and persist it on the event
            # so the LLM sees it directly without calling read_file.
            attachment.size = real_size  # populate for the helper
            inlined_text: str | None = None
            inlined_kind: str | None = None
            skip_reason: str | None = None
            inline_kind = _inline_extension_kind(attachment.filename)
            if (
                real_size <= _INLINE_MAX_BYTES
                and inline_kind is not None
            ):
                try:
                    raw_bytes = await storage.read(bucket, storage_key)
                except KeyError:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            "Attachment path not found in workspace: "
                            f"{attachment.path}"
                        ),
                    )
                document_path: Path | None = None
                if inline_kind == "document":
                    local_candidate = (
                        Path(storage.resolve_bucket_path(bucket))
                        / storage_key
                    )
                    if local_candidate.is_file():
                        document_path = local_candidate
                    else:
                        suffix = (
                            os.path.splitext(attachment.filename)[1].lower()
                        )
                        modified = str(stat.get("modified") or "")
                        document_path = _materialize_for_cache(
                            raw_bytes=raw_bytes,
                            bucket=bucket,
                            storage_key=storage_key,
                            size=real_size,
                            modified=modified,
                            suffix=suffix,
                        )
                inlined_text, inlined_kind, skip_reason = (
                    await _try_inline_attachment(
                        attachment, raw_bytes, document_path,
                    )
                )

            entry: dict[str, Any] = {
                "path": attachment.path,
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "size": real_size,
            }
            if inlined_text is not None:
                entry["inlined_text"] = inlined_text
                entry["inlined_render_kind"] = inlined_kind
                logger.info(
                    "event=attachment.inline result=ok kind=%s "
                    "filename=%s bytes=%d rendered_chars=%d",
                    inlined_kind, attachment.filename, real_size,
                    len(inlined_text),
                )
            elif skip_reason is not None:
                entry["inline_skip_reason"] = skip_reason
            resolved.append(entry)
        attachments_payload = resolved
```

- [ ] **Step 3: Run the existing attachment tests to ensure nothing regressed**

```bash
cd /work/surogates && uv run pytest tests/ -k "attachment" -v 2>&1 | /usr/bin/tail -25
```

Expected: pre-existing tests still pass; new unit tests still pass.

- [ ] **Step 4: Commit**

```bash
cd /work/surogates
git add surogates/api/routes/sessions.py
git commit -m "feat(api): wire inline parsing into send-message attachment loop

For each resolved attachment under the 2 MB inline cap and of a
supported type, the route now fetches the bytes from storage,
uses the local workspace file or a deterministic materialised temp path
for document-cache stability, and calls _try_inline_attachment.  The result (inlined_text +
render_kind, or inline_skip_reason) is persisted on the user.message
event under the attachment entry."
```

---

## Task 6: `_render_inlined_attachments` renderer

A pure function in `harness/loop.py` that takes the user's content string and the persisted attachment list and returns the content augmented with one fenced block per inlined attachment.

**Files:**
- Modify: `surogates/harness/loop.py` (add the helper near `_attachments_note` around line 220)
- Create: `tests/harness/__init__.py` (empty marker if not present)
- Create: `tests/harness/test_attachment_rendering.py`

- [ ] **Step 1: Create the test package marker**

```bash
mkdir -p /work/surogates/tests/harness
test -f /work/surogates/tests/harness/__init__.py || touch /work/surogates/tests/harness/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `/work/surogates/tests/harness/test_attachment_rendering.py`:

```python
"""Unit tests for the inline-attachment renderer in harness/loop.py."""

from __future__ import annotations


def test_render_inlined_appends_fenced_block_for_markdown_kind() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    out = _render_inlined_attachments(
        "summarise this",
        [
            {
                "path": "uploads/report.pdf",
                "filename": "report.pdf",
                "inlined_text": "# Heading\n| col1 | col2 |\n| ---- | ---- |\n| a | b |",
                "inlined_render_kind": "markdown",
            }
        ],
    )
    assert out.startswith("summarise this")
    assert "**Attachment: report.pdf**" in out
    assert "parsed via markitdown/pymupdf4llm" in out
    assert "read_file(\"uploads/report.pdf\")" in out
    assert "# Heading" in out


def test_render_inlined_text_kind_omits_parser_subtitle() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    out = _render_inlined_attachments(
        "look at this",
        [
            {
                "path": "uploads/notes.md",
                "filename": "notes.md",
                "inlined_text": "# notes\nhello",
                "inlined_render_kind": "text",
            }
        ],
    )
    assert "**Attachment: notes.md**" in out
    assert "parsed via markitdown" not in out  # no parser subtitle for text
    assert "# notes" in out


def test_render_inlined_handles_multiple_attachments_in_order() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    out = _render_inlined_attachments(
        "compare these",
        [
            {
                "path": "uploads/a.pdf", "filename": "a.pdf",
                "inlined_text": "ALPHA", "inlined_render_kind": "markdown",
            },
            {
                "path": "uploads/b.pdf", "filename": "b.pdf",
                "inlined_text": "BRAVO", "inlined_render_kind": "markdown",
            },
        ],
    )
    assert out.index("ALPHA") < out.index("BRAVO")
    assert out.index("**Attachment: a.pdf**") < out.index("**Attachment: b.pdf**")


def test_render_inlined_skips_path_only_attachments() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    out = _render_inlined_attachments(
        "look at these",
        [
            {
                "path": "uploads/inlined.pdf", "filename": "inlined.pdf",
                "inlined_text": "INLINED", "inlined_render_kind": "markdown",
            },
            {
                # No inlined_text → should not appear in the rendered text.
                "path": "uploads/huge.pdf", "filename": "huge.pdf",
            },
        ],
    )
    assert "INLINED" in out
    assert "huge.pdf" not in out


def test_render_inlined_returns_content_unchanged_when_nothing_to_inline() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    assert _render_inlined_attachments("hi", []) == "hi"
    assert _render_inlined_attachments("hi", None) == "hi"
    assert _render_inlined_attachments("hi", [{"path": "x", "filename": "x"}]) == "hi"
```

- [ ] **Step 3: Run (expect ImportError on every test)**

```bash
cd /work/surogates && uv run pytest tests/harness/test_attachment_rendering.py -v
```

Expected: ImportError on every test.

- [ ] **Step 4: Implement the renderer**

In `/work/surogates/surogates/harness/loop.py`, add the function just below `_attachments_note` (around line 277):

```python
def _render_inlined_attachments(
    content: str,
    attachments: list[Any] | None,
) -> str:
    """Append one fenced block per inlined attachment to ``content``.

    ``attachments`` is the persisted ``data["attachments"]`` payload
    from a ``user.message`` event.  Each item with a non-empty
    ``inlined_text`` field becomes a fenced block at the end of the
    returned string.  Items without ``inlined_text`` (path-only,
    inline-skipped, or unsupported) are ignored here -- the system
    ``_attachments_note`` surface covers them.
    """
    if not attachments:
        return content
    blocks: list[str] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        inlined = item.get("inlined_text")
        if not inlined:
            continue
        kind = item.get("inlined_render_kind") or "text"
        path = item.get("path") or ""
        filename = item.get("filename") or path
        header = f"**Attachment: {filename}**"
        if kind == "markdown":
            subtitle = (
                "*(parsed via markitdown/pymupdf4llm — to re-read or "
                f"paginate, use `read_file(\"{path}\")`)*"
            )
            block = f"---\n{header}\n{subtitle}\n\n{inlined}\n---"
        else:
            block = f"---\n{header}\n\n{inlined}\n---"
        blocks.append(block)
    if not blocks:
        return content
    return content + "\n\n" + "\n\n".join(blocks)
```

- [ ] **Step 5: Run the tests (expect PASS)**

```bash
cd /work/surogates && uv run pytest tests/harness/test_attachment_rendering.py -v
```

Expected: all 5 PASS.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop.py tests/harness/__init__.py tests/harness/test_attachment_rendering.py
git commit -m "feat(harness): _render_inlined_attachments renderer

Pure-function helper that appends one fenced block per attachment
carrying inlined_text.  markdown kind gets the parser+read_file
re-read subtitle; text kind gets the bare header (no parser involved
for plain text)."
```

---

## Task 7: Call the renderer in `_rebuild_messages`

When replaying a `USER_MESSAGE` event into LLM history, augment the content with `_render_inlined_attachments` so the inlined blocks appear in every history rebuild (live or after restart).

**Files:**
- Modify: [surogates/harness/loop.py:3916-3941](../../../surogates/harness/loop.py) (`USER_MESSAGE` branch)

- [ ] **Step 1: Read the current branch**

```bash
cd /work/surogates && /bin/sed -n '3916,3942p' surogates/harness/loop.py
```

- [ ] **Step 2: Replace the branch**

Replace the body of the `USER_MESSAGE` branch in `_rebuild_messages`:

```python
            if etype == EventType.USER_MESSAGE.value:
                content = event.data.get("content", "")
                content = _render_inlined_attachments(
                    content, event.data.get("attachments"),
                )
                images = event.data.get("images")
                if images:
                    logger.info(
                        "User message has %d image(s), first mime: %s",
                        len(images),
                        images[0].get("mime_type", "?"),
                    )
                if images:
                    blocks: list[dict] = [{"type": "text", "text": content}]
                    for img in images:
                        data_url = img["data"]
                        if not data_url.startswith("data:"):
                            mime = img.get("mime_type", "image/png")
                            data_url = f"data:{mime};base64,{data_url}"
                        blocks.append({
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "auto"},
                        })
                    user_msg = {"role": "user", "content": blocks}
                    from surogates.harness.image_shrink import shrink_image_parts_in_messages
                    shrink_image_parts_in_messages([user_msg])
                    messages.append(user_msg)
                else:
                    messages.append({"role": "user", "content": content})
```

The only change is the inserted line: `content = _render_inlined_attachments(content, event.data.get("attachments"))`. The rest is unchanged from the existing code so the image branch keeps working.

- [ ] **Step 3: Write a quick rebuild test**

Append to `/work/surogates/tests/harness/test_attachment_rendering.py`:

```python
def test_rebuild_messages_inlines_attachment_into_user_message() -> None:
    from types import SimpleNamespace

    from surogates.harness.loop import AgentHarness
    from surogates.session.events import EventType

    # Minimal Event-shaped stub.
    user_event = SimpleNamespace(
        type=EventType.USER_MESSAGE.value,
        data={
            "content": "summarise this",
            "attachments": [
                {
                    "path": "uploads/r.pdf",
                    "filename": "r.pdf",
                    "inlined_text": "INLINE BODY",
                    "inlined_render_kind": "markdown",
                }
            ],
        },
        id=1,
    )

    # _rebuild_messages is a method; call it with a minimal self.
    self_stub = SimpleNamespace()
    messages = AgentHarness._rebuild_messages(self_stub, [user_event])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    text = messages[0]["content"]
    assert isinstance(text, str)
    assert "summarise this" in text
    assert "**Attachment: r.pdf**" in text
    assert "INLINE BODY" in text
```

If `_rebuild_messages` is a bound method that uses `self._format_advisor_context`, the stub call may fail — in that case adjust the test to provide a stub `_format_advisor_context = lambda *a, **k: ""`. The test above does not exercise the advisor branch, so the stub should work as-is.

- [ ] **Step 4: Run the test (expect PASS)**

```bash
cd /work/surogates && uv run pytest tests/harness/test_attachment_rendering.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop.py tests/harness/test_attachment_rendering.py
git commit -m "feat(harness): render inlined attachments into LLM user messages

_rebuild_messages now passes the persisted attachments list through
_render_inlined_attachments before building the user content.  The
inlined blocks therefore appear in every replay -- live turns and
post-restart history rebuilds alike."
```

---

## Task 8: Revise `_attachments_note`

Filter inlined attachments out of the system note; surface skip reasons for path-only entries that *were* candidates.

**Files:**
- Modify: [surogates/harness/loop.py:221-276](../../../surogates/harness/loop.py)
- Modify: `tests/harness/test_attachment_rendering.py`

- [ ] **Step 1: Add the failing tests**

Append to `/work/surogates/tests/harness/test_attachment_rendering.py`:

```python
def test_attachments_note_returns_none_when_all_inlined() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _attachments_note
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={
                "content": "x",
                "attachments": [
                    {
                        "path": "uploads/a.pdf", "filename": "a.pdf",
                        "mime_type": "application/pdf", "size": 1000,
                        "inlined_text": "BODY", "inlined_render_kind": "markdown",
                    }
                ],
            },
            id=1,
        ),
    ]
    assert _attachments_note(events) is None


def test_attachments_note_lists_only_path_only_attachments() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _attachments_note
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={
                "content": "x",
                "attachments": [
                    {
                        "path": "uploads/inlined.pdf",
                        "filename": "inlined.pdf",
                        "mime_type": "application/pdf", "size": 1000,
                        "inlined_text": "BODY",
                        "inlined_render_kind": "markdown",
                    },
                    {
                        "path": "uploads/big.pdf",
                        "filename": "big.pdf",
                        "mime_type": "application/pdf",
                        "size": 9_000_000,
                    },
                ],
            },
            id=1,
        ),
    ]
    note = _attachments_note(events)
    assert note is not None
    assert "big.pdf" in note
    assert "inlined.pdf" not in note


def test_attachments_note_includes_skip_reason_diagnostic() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _attachments_note
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={
                "content": "x",
                "attachments": [
                    {
                        "path": "uploads/corrupt.pdf",
                        "filename": "corrupt.pdf",
                        "mime_type": "application/pdf",
                        "size": 1000,
                        "inline_skip_reason": "parse_error",
                    },
                ],
            },
            id=1,
        ),
    ]
    note = _attachments_note(events)
    assert note is not None
    assert "corrupt.pdf" in note
    assert "parse_error" in note
    assert "read_file" in note.lower()
```

- [ ] **Step 2: Run the tests (expect failure on filter + skip-reason behavior)**

```bash
cd /work/surogates && uv run pytest tests/harness/test_attachment_rendering.py -v -k "attachments_note"
```

Expected: failures on the new tests.

- [ ] **Step 3: Replace `_attachments_note`**

Replace the body of `_attachments_note` at `/work/surogates/surogates/harness/loop.py` with:

```python
def _attachments_note(events: list[Any]) -> str | None:
    """Return a per-turn system note describing path-only attachments.

    Reads ``data.attachments`` on the most recent ``user.message``
    event.  Any attachment whose ``inlined_text`` is already populated
    is omitted from this note (the content lives in the user message
    text via ``_render_inlined_attachments``).  Attachments that were
    candidates for inline but skipped get an annotated entry that names
    the ``inline_skip_reason`` so the agent knows why it needs to fall
    back to ``read_file``.
    """
    latest_attachments: list[Any] | None = None
    for event in reversed(events):
        event_type = event.type
        type_value = (
            event_type.value if hasattr(event_type, "value") else event_type
        )
        if type_value != EventType.USER_MESSAGE.value:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        candidate = data.get("attachments")
        if isinstance(candidate, list):
            latest_attachments = candidate
        break

    if not latest_attachments:
        return None

    lines = [
        "The user attached the following files to this message. They are"
        " available in the workspace and you can read them with your file"
        " tools:",
    ]
    for item in latest_attachments:
        if not isinstance(item, dict):
            continue
        if item.get("inlined_text"):
            continue  # content already in the user message text
        path = item.get("path")
        filename = item.get("filename")
        if not path or not filename:
            continue
        mime = item.get("mime_type") or "application/octet-stream"
        raw_size = item.get("size")
        if isinstance(raw_size, (int, float)) and raw_size >= 0:
            size_str = _format_bytes(int(raw_size))
        else:
            size_str = "unknown size"
        line = f"- {path} ({mime}, {size_str}) — \"{filename}\""
        skip_reason = item.get("inline_skip_reason")
        if skip_reason:
            hint = _ATTACHMENT_SKIP_HINTS.get(skip_reason, "use read_file")
            line += f" (inline skipped: {skip_reason} — {hint})"
        lines.append(line)

    if len(lines) == 1:
        return None
    return "\n".join(lines)
```

Add the hint table near `_attachments_note` (above the function, after the existing `_format_bytes` helper):

```python
_ATTACHMENT_SKIP_HINTS: dict[str, str] = {
    "parse_error": (
        "try read_file with pdftotext/pandoc fallbacks"
    ),
    "parse_timeout": (
        "the parser hit the 30 s cap; try read_file with a narrower offset/limit"
    ),
    "decode_error": (
        "the file is not UTF-8; try read_file which has full BOM detection"
    ),
    "oversize_output": (
        "the parsed content exceeded the inline cap; use read_file with"
        " offset/limit to paginate"
    ),
    "empty_output": (
        "the parser produced no text; the file may be a scan — try the"
        " ocr-and-documents skill"
    ),
}
```

- [ ] **Step 4: Run the tests (expect PASS)**

```bash
cd /work/surogates && uv run pytest tests/harness/test_attachment_rendering.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop.py tests/harness/test_attachment_rendering.py
git commit -m "feat(harness): filter inlined attachments out of the system note

Attachments whose content is already in the user message text are
suppressed from _attachments_note.  Attachments that were candidates
for inline but skipped get the inline_skip_reason surfaced with a
hint that points the agent at the right fallback (pdftotext/pandoc,
narrower read_file window, ocr-and-documents skill)."
```

---

## Task 9: Integration test — round-trip via the rebuild pipeline

End-to-end check: create attachment files on disk, build a `USER_MESSAGE` event the way the route would, run `_rebuild_messages`, assert the LLM-visible user message contains the inlined content.

**Files:**
- Create: `tests/integration/test_inline_attachments_e2e.py`

- [ ] **Step 1: Write the test**

Create `/work/surogates/tests/integration/test_inline_attachments_e2e.py`:

```python
"""End-to-end: inlined attachments appear in the rebuilt LLM user message."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from surogates.api.routes.sessions import (
    AttachmentRef,
    _materialize_for_cache,
    _try_inline_attachment,
)
from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from tests.tools.fixtures.build_documents import (
    build_minimal_docx,
    build_minimal_pdf,
    build_minimal_pptx,
    build_minimal_xlsx,
)


@pytest.fixture
def isolated_document_cache(tmp_path, monkeypatch):
    from surogates.tools.utils import document_cache as cache_module
    fresh = cache_module.DocumentCache(
        root=tmp_path / "doc-cache",
        max_entries=8,
        max_entry_bytes=2 * 1024 * 1024,
    )
    monkeypatch.setattr(cache_module, "_DEFAULT", fresh)
    return fresh


@pytest.mark.parametrize(
    "builder, filename, probe",
    [
        (build_minimal_pdf, "doc.pdf", "Hello PDF"),
        (build_minimal_docx, "doc.docx", "Hello DOCX"),
        (build_minimal_xlsx, "doc.xlsx", "Alpha"),
        (build_minimal_pptx, "doc.pptx", "Hello PPTX"),
    ],
)
@pytest.mark.asyncio
async def test_round_trip_inlines_document_into_user_message(
    tmp_path: Path,
    isolated_document_cache,
    builder,
    filename: str,
    probe: str,
) -> None:
    """Build a fixture, simulate the route's inline step, and assert the
    rebuilt LLM user message text contains the parsed content.
    """
    source = builder(tmp_path / filename)
    materialised = _materialize_for_cache(
        raw_bytes=source.read_bytes(),
        bucket="bucket",
        storage_key=f"sess/uploads/{filename}",
        size=source.stat().st_size,
        modified="2026-05-25T05:00:00Z",
        suffix=source.suffix,
        cache_root=tmp_path / "materialised",
    )
    attachment = AttachmentRef(
        path=f"uploads/{filename}", filename=filename,
        size=source.stat().st_size,
    )
    text, kind, reason = await _try_inline_attachment(
        attachment, source.read_bytes(), materialised,
    )
    assert reason is None
    assert kind == "markdown"
    assert text and probe in text

    user_event = SimpleNamespace(
        type=EventType.USER_MESSAGE.value,
        data={
            "content": "summarise this",
            "attachments": [
                {
                    "path": attachment.path,
                    "filename": attachment.filename,
                    "size": attachment.size,
                    "inlined_text": text,
                    "inlined_render_kind": kind,
                }
            ],
        },
        id=1,
    )
    messages = AgentHarness._rebuild_messages(SimpleNamespace(), [user_event])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert isinstance(content, str)
    assert "summarise this" in content
    assert f"**Attachment: {filename}**" in content
    assert probe in content


@pytest.mark.asyncio
async def test_round_trip_inlines_plain_text(tmp_path: Path) -> None:
    src = tmp_path / "notes.md"
    src.write_bytes(b"# title\nhello world\n")
    attachment = AttachmentRef(
        path="uploads/notes.md", filename="notes.md",
        size=src.stat().st_size,
    )
    text, kind, reason = await _try_inline_attachment(
        attachment, src.read_bytes(), src,
    )
    assert kind == "text"
    assert reason is None

    user_event = SimpleNamespace(
        type=EventType.USER_MESSAGE.value,
        data={
            "content": "look at this",
            "attachments": [
                {
                    "path": attachment.path,
                    "filename": attachment.filename,
                    "size": attachment.size,
                    "inlined_text": text,
                    "inlined_render_kind": kind,
                }
            ],
        },
        id=1,
    )
    messages = AgentHarness._rebuild_messages(SimpleNamespace(), [user_event])
    content = messages[0]["content"]
    assert "**Attachment: notes.md**" in content
    assert "# title" in content
    # text kind should not get the parser subtitle.
    assert "parsed via markitdown" not in content
```

- [ ] **Step 2: Run the test**

```bash
cd /work/surogates && uv run pytest tests/integration/test_inline_attachments_e2e.py -v
```

Expected: all parametrised cases PASS.

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add tests/integration/test_inline_attachments_e2e.py
git commit -m "test(integration): inline attachments round-trip end-to-end

Builds fixtures for pdf/docx/xlsx/pptx/md, runs them through
_try_inline_attachment, packs them into a synthetic USER_MESSAGE event,
and asserts _rebuild_messages produces an LLM-visible user message
that contains the parsed content under the expected fenced block."
```

---

## Task 10: History-replay regression test

Critical because we're persisting parser output into events. The same event payload, rebuilt twice (live + post-restart), must produce bit-for-bit identical LLM messages.

**Files:**
- Create: `tests/integration/test_attachment_history_replay.py`

- [ ] **Step 1: Write the test**

Create `/work/surogates/tests/integration/test_attachment_history_replay.py`:

```python
"""History-replay determinism for inlined attachments.

If the persisted user.message event carries inlined_text, every
rebuild of the conversation history must produce the exact same LLM
user content — no re-parsing, no drift.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from surogates.api.routes.sessions import (
    AttachmentRef,
    _try_inline_attachment,
)
from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from tests.tools.fixtures.build_documents import build_minimal_pdf


@pytest.fixture
def isolated_document_cache(tmp_path, monkeypatch):
    from surogates.tools.utils import document_cache as cache_module
    fresh = cache_module.DocumentCache(
        root=tmp_path / "doc-cache",
        max_entries=8,
        max_entry_bytes=2 * 1024 * 1024,
    )
    monkeypatch.setattr(cache_module, "_DEFAULT", fresh)
    return fresh


@pytest.mark.asyncio
async def test_replay_produces_identical_user_content(
    tmp_path: Path, isolated_document_cache, monkeypatch,
) -> None:
    src = build_minimal_pdf(tmp_path / "x.pdf", heading="Replay Probe")
    attachment = AttachmentRef(
        path="uploads/x.pdf", filename="x.pdf", size=src.stat().st_size,
    )
    text, kind, _ = await _try_inline_attachment(
        attachment, src.read_bytes(), src,
    )
    assert text is not None
    assert "Replay Probe" in text

    event = SimpleNamespace(
        type=EventType.USER_MESSAGE.value,
        data={
            "content": "summarise this",
            "attachments": [
                {
                    "path": attachment.path,
                    "filename": attachment.filename,
                    "size": attachment.size,
                    "inlined_text": text,
                    "inlined_render_kind": kind,
                }
            ],
        },
        id=1,
    )

    first = AgentHarness._rebuild_messages(SimpleNamespace(), [event])
    second = AgentHarness._rebuild_messages(SimpleNamespace(), [event])
    assert first == second
    assert isinstance(first[0]["content"], str)
    assert "Replay Probe" in first[0]["content"]

    # Mutate the cache after the event was persisted and rebuild again.
    # The replay must still produce identical output because rebuild
    # never reparses — it reads inlined_text straight off the event.
    call_count = {"n": 0}
    from surogates.tools.builtin import file_ops

    real_parser = file_ops._parse_document_to_markdown

    async def counting(p):
        call_count["n"] += 1
        return await real_parser(p)

    monkeypatch.setattr(file_ops, "_parse_document_to_markdown", counting)

    third = AgentHarness._rebuild_messages(SimpleNamespace(), [event])
    assert third == first
    assert call_count["n"] == 0, (
        "replay should not re-invoke the parser"
    )
```

- [ ] **Step 2: Run the test (expect PASS)**

```bash
cd /work/surogates && uv run pytest tests/integration/test_attachment_history_replay.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add tests/integration/test_attachment_history_replay.py
git commit -m "test(integration): attachment history replay is deterministic

Builds a PDF, inlines it, rebuilds the conversation twice -- once
fresh, once after the parser is monkeypatched to a counter -- and
asserts both rebuilds produce identical LLM messages and the parser
is never re-invoked.  Guards against any future change that would
re-parse attachments during replay."
```

---

## Task 11: Update `docs/tools/index.md`

One-paragraph note pointing at the new behaviour so human readers (and any future review of the agent docs) understand that the send-message route auto-inlines small attachments.

**Files:**
- Modify: `docs/tools/index.md`

- [ ] **Step 1: Locate the read_file section**

```bash
cd /work/surogates && /bin/grep -n "read_file" docs/tools/index.md | head -5
```

- [ ] **Step 2: Append a paragraph below the existing read_file paragraph**

In `/work/surogates/docs/tools/index.md`, immediately after the existing paragraph that begins "Handles plain text plus .pdf, .docx, .xlsx, .pptx (parsed to markdown via markitdown)…", add:

```markdown

For attachments uploaded with the user message itself (via the chat UI),
the harness now parses files under 2 MB at send time and inlines the
parsed markdown directly into the user message — the agent receives
the content without making an extra `read_file` call. Files larger
than 2 MB, files in unsupported formats, and files that fail to parse
fall back to the previous behaviour: a system note tells the agent
the file is in the workspace and `read_file` is the way to access it.
```

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add docs/tools/index.md
git commit -m "docs(tools): describe send-time inline attachment behaviour"
```

---

## Task 12: Smoke test the whole stack and verify

Run the suite end-to-end. Lint. Confirm nothing else regressed.

- [ ] **Step 1: Run the new + adjacent tests**

```bash
cd /work/surogates && uv run pytest tests/api/test_attachment_inlining.py tests/harness/test_attachment_rendering.py tests/integration/test_inline_attachments_e2e.py tests/integration/test_attachment_history_replay.py -v 2>&1 | /usr/bin/tail -30
```

Expected: all PASS.

- [ ] **Step 2: Run a wider regression sweep**

```bash
cd /work/surogates && uv run pytest tests/ --ignore=tests/integration --ignore=tests/missions --ignore=tests/tasks -q 2>&1 | /usr/bin/tail -5
```

Expected: same pass count as before the work plus the new unit tests; zero new failures.

- [ ] **Step 3: Lint the changed files**

```bash
cd /work/surogates && uv run ruff check surogates/api/routes/sessions.py surogates/harness/loop.py 2>&1 | /usr/bin/tail -20
```

Expected: no new findings. Pre-existing E402 errors on `surogates/harness/loop.py` and on the sessions module are not blockers — they were there before the work and are outside the scope of this PR.

- [ ] **Step 4: Update the task tracker at the top of the plan**

Mark every task `[x]` in the tracker section of `/work/surogates/docs/superpowers/plans/2026-05-25-inline-attachments.md`.

- [ ] **Step 5: Final commit (only if anything needed touch-up)**

If steps 1-3 forced edits, commit them. Otherwise skip.

```bash
cd /work/surogates
git add -A
git commit -m "chore: address smoke-test findings for inline attachments"
```

---

## Notes for the implementer

- **The 2 MB cap is on the raw file** (after storage `stat`), **not on the rendered markdown.** The 200 KB secondary cap on rendered output catches the edge case of a small file producing huge markdown.
- **Materialisation under `/tmp/surogates-attachment-inline/`** stays around for the lifetime of the pod. The cache directory is shared with the existing document cache at `/tmp/surogates-read-cache/documents/` only conceptually — they are separate paths to keep the cleanup story simple.
- **The persisted event is the source of truth for replay.** Do not be tempted to "rehydrate" attachments by re-parsing on rebuild; that breaks the history-replay determinism the regression test enforces.
- **Image attachments are out of scope for this plan.** They continue to flow through `ImageBlock` and `_prepare_messages_for_model_vision_support`. Touching that surface should be a separate spec.
- **Storage `read` may be expensive on S3/R2** (one network round-trip per attachment). The cap check on `real_size` runs *before* `storage.read`, so files over 2 MB never trigger a fetch.
