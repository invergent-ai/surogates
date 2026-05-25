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
