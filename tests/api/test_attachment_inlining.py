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
