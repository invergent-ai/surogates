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


# ---------------------------------------------------------------------------
# Task 4 — _try_inline_attachment unit tests
# ---------------------------------------------------------------------------


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
async def test_try_inline_attachment_pdf_returns_markdown(
    tmp_path, isolated_document_cache,
) -> None:
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
    tmp_path, monkeypatch, isolated_document_cache,
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
    tmp_path, monkeypatch, isolated_document_cache,
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
    tmp_path,
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
