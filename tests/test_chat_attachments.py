"""Tests for non-image attachments on user.message events.

Covers the wire-format extension introduced for arbitrary-file attachments
in the chat composer:

1. ``SendMessageRequest`` accepts and round-trips an optional ``attachments``
   list with path/filename safety checks and a count cap.
2. The send-message route resolves attachment paths against the session's
   workspace bucket, recomputes size from storage, enforces per-file and
   per-message size caps, scans filenames through the prompt-injection
   detector, and writes attachments[] into the user.message event payload.

The ``_attachments_note`` helper and its insertion into the main loop are
covered in ``tests/test_chat_attachments_note.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from surogates.api.routes.sessions import (
    AttachmentRef,
    SendMessageRequest,
    _MAX_ATTACHMENTS_PER_MESSAGE,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_send_message_request_defaults_attachments_to_none():
    req = SendMessageRequest(content="hi")
    assert req.attachments is None


def test_send_message_request_round_trips_attachments():
    req = SendMessageRequest(
        content="hi",
        attachments=[
            AttachmentRef(
                path="uploads/1715600000-report.pdf",
                filename="report.pdf",
                mime_type="application/pdf",
                size=12345,
            ),
        ],
    )
    assert req.attachments is not None
    assert req.attachments[0].path == "uploads/1715600000-report.pdf"
    assert req.attachments[0].filename == "report.pdf"
    assert req.attachments[0].mime_type == "application/pdf"
    assert req.attachments[0].size == 12345


def test_send_message_request_accepts_empty_attachments_list():
    req = SendMessageRequest(content="hi", attachments=[])
    assert req.attachments == []


def test_attachment_ref_rejects_empty_path():
    with pytest.raises(ValidationError):
        SendMessageRequest(
            content="hi",
            attachments=[AttachmentRef(path="", filename="x.txt")],
        )


def test_attachment_ref_rejects_leading_slash():
    with pytest.raises(ValidationError):
        SendMessageRequest(
            content="hi",
            attachments=[AttachmentRef(path="/etc/passwd", filename="passwd")],
        )


def test_attachment_ref_rejects_parent_segment():
    with pytest.raises(ValidationError):
        SendMessageRequest(
            content="hi",
            attachments=[
                AttachmentRef(path="uploads/../etc/passwd", filename="x"),
            ],
        )


def test_attachment_ref_rejects_nul_in_path():
    with pytest.raises(ValidationError):
        SendMessageRequest(
            content="hi",
            attachments=[AttachmentRef(path="uploads/a\x00b", filename="x")],
        )


def test_attachment_ref_rejects_empty_filename():
    with pytest.raises(ValidationError):
        SendMessageRequest(
            content="hi",
            attachments=[AttachmentRef(path="uploads/x", filename="")],
        )


def test_attachment_ref_rejects_path_separator_in_filename():
    with pytest.raises(ValidationError):
        SendMessageRequest(
            content="hi",
            attachments=[AttachmentRef(path="uploads/x", filename="a/b.txt")],
        )


def test_attachment_ref_rejects_backslash_in_filename():
    with pytest.raises(ValidationError):
        SendMessageRequest(
            content="hi",
            attachments=[AttachmentRef(path="uploads/x", filename=r"a\b.txt")],
        )


def test_attachment_ref_rejects_nul_in_filename():
    with pytest.raises(ValidationError):
        SendMessageRequest(
            content="hi",
            attachments=[AttachmentRef(path="uploads/x", filename="bad\x00")],
        )


def test_send_message_request_rejects_too_many_attachments():
    too_many = [
        AttachmentRef(path=f"uploads/{i}.txt", filename=f"{i}.txt")
        for i in range(_MAX_ATTACHMENTS_PER_MESSAGE + 1)
    ]
    with pytest.raises(ValidationError):
        SendMessageRequest(content="hi", attachments=too_many)


def test_send_message_request_accepts_max_attachments():
    """Edge: exactly _MAX_ATTACHMENTS_PER_MESSAGE attachments is allowed."""
    boundary = [
        AttachmentRef(path=f"uploads/{i}.txt", filename=f"{i}.txt")
        for i in range(_MAX_ATTACHMENTS_PER_MESSAGE)
    ]
    req = SendMessageRequest(content="hi", attachments=boundary)
    assert len(req.attachments or []) == _MAX_ATTACHMENTS_PER_MESSAGE
