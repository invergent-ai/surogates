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

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from surogates.api.routes.sessions import (
    AttachmentRef,
    SendMessageRequest,
    _MAX_ATTACHMENT_BYTES,
    _MAX_ATTACHMENTS_PER_MESSAGE,
    _MAX_ATTACHMENTS_TOTAL_BYTES,
    send_message,
)
from surogates.session.events import EventType


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


# ---------------------------------------------------------------------------
# Route: send_message with attachments
# ---------------------------------------------------------------------------


def _stub_session(*, status: str = "active"):
    """Build a Session-like object the route helpers will accept."""
    return SimpleNamespace(
        id=uuid4(),
        agent_id="agent-1",
        status=status,
        channel="web",
        tenant_id="tenant-1",
        org_id="org-1",
        model=None,
        config={"storage_bucket": "test-bucket"},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        title=None,
    )


class _StubInjectionDetector:
    """Captures every string fed to the detector, never flags injection."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def detect(self, content: str, *, source: str):
        self.calls.append((content, source))
        return SimpleNamespace(is_injection=False, explanation="")


class _BlockingInjectionDetector:
    """Flags any string containing ``BLOCK_ME``."""

    def detect(self, content: str, *, source: str):
        if "BLOCK_ME" in content:
            return SimpleNamespace(
                is_injection=True,
                explanation="contains BLOCK_ME marker",
            )
        return SimpleNamespace(is_injection=False, explanation="")


class _FakeStorage:
    """Async storage stub keyed on workspace-relative paths."""

    def __init__(self, files: dict[str, int]) -> None:
        # Keys are the storage-prefixed paths, values are sizes.
        self._files = files

    async def exists(self, _bucket: str, key: str) -> bool:
        return key in self._files

    async def stat(self, _bucket: str, key: str) -> dict:
        if key not in self._files:
            raise KeyError(key)
        return {"size": self._files[key], "modified": 0.0}


@pytest.fixture
def patched_send_message(monkeypatch):
    """Drive the send_message route with stubbed storage + store.

    Returns a callable that takes ``attachments`` (list of dicts) and
    ``workspace_files`` (mapping ``relative_path -> size_in_bytes``) and
    runs the route end-to-end, returning ``(result, store, detector)``.

    The fake ``prefixed_session_workspace_key`` is the identity function on the
    user-supplied path, so workspace_files keys are the same as the paths
    the client sends in.  This keeps the test's mental model simple
    without coupling it to the real session/root-id prefix.
    """

    base_detector = _StubInjectionDetector()
    monkeypatch.setattr(
        "surogates.api.routes.sessions._get_injection_detector",
        lambda: base_detector,
    )

    async def _runner(
        *,
        attachments: list[dict] | None,
        workspace_files: dict[str, int],
        content: str = "please summarize",
        detector_override=None,
    ):
        active_detector = detector_override or base_detector
        monkeypatch.setattr(
            "surogates.api.routes.sessions._get_injection_detector",
            lambda: active_detector,
        )

        session = _stub_session()
        store = SimpleNamespace(
            emit_event=AsyncMock(return_value=99),
            update_session_status=AsyncMock(return_value=None),
        )

        async def _get_session_for_tenant(*_args, **_kwargs):
            return session

        monkeypatch.setattr(
            "surogates.api.routes.sessions._get_session_for_tenant",
            _get_session_for_tenant,
        )
        monkeypatch.setattr(
            "surogates.api.routes.sessions._get_session_store",
            lambda _req: store,
        )

        monkeypatch.setattr(
            "surogates.api.routes.sessions._get_storage",
            lambda _req: _FakeStorage(workspace_files),
        )

        async def _get_bucket_and_root(_store, _sid, _tenant):
            return session, "test-bucket", "root/"

        monkeypatch.setattr(
            "surogates.api.routes.sessions"
            "._get_workspace_session_bucket_and_root",
            _get_bucket_and_root,
        )
        monkeypatch.setattr(
            "surogates.api.routes.sessions.prefixed_session_workspace_key",
            lambda _config, _root_id, path: path,
        )

        monkeypatch.setattr(
            "surogates.api.routes.sessions.enqueue_session",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "surogates.api.routes.sessions.require_user_writable_session",
            lambda _session: None,
        )

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=None)),
            url=SimpleNamespace(path="/sessions/x/messages"),
        )

        body = SendMessageRequest(
            content=content,
            attachments=(
                [AttachmentRef(**a) for a in attachments] if attachments else None
            ),
        )

        result = await send_message(
            session_id=session.id,
            body=body,
            request=request,  # type: ignore[arg-type]
            tenant=SimpleNamespace(),  # type: ignore[arg-type]
        )
        return result, store, active_detector

    return _runner


@pytest.mark.asyncio
async def test_send_message_persists_attachments_with_real_storage_size(
    patched_send_message,
):
    _, store, _ = await patched_send_message(
        attachments=[
            {
                "path": "uploads/1715600000-report.pdf",
                "filename": "report.pdf",
                "mime_type": "application/pdf",
                "size": 1,  # bogus client hint — harness must overwrite
            },
        ],
        workspace_files={"uploads/1715600000-report.pdf": 12345},
    )

    store.emit_event.assert_called_once()
    args, _ = store.emit_event.call_args
    _sid, event_type, event_data = args
    assert event_type == EventType.USER_MESSAGE
    assert event_data["attachments"] == [
        {
            "path": "uploads/1715600000-report.pdf",
            "filename": "report.pdf",
            "mime_type": "application/pdf",
            "size": 12345,
        },
    ]


@pytest.mark.asyncio
async def test_send_message_rejects_missing_attachment_path(
    patched_send_message,
):
    with pytest.raises(HTTPException) as ei:
        await patched_send_message(
            attachments=[
                {
                    "path": "uploads/does-not-exist.pdf",
                    "filename": "missing.pdf",
                },
            ],
            workspace_files={},
        )
    assert ei.value.status_code == 422
    assert "uploads/does-not-exist.pdf" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_send_message_rejects_oversize_attachment(patched_send_message):
    with pytest.raises(HTTPException) as ei:
        await patched_send_message(
            attachments=[
                {"path": "uploads/huge.bin", "filename": "huge.bin"},
            ],
            workspace_files={"uploads/huge.bin": _MAX_ATTACHMENT_BYTES + 1},
        )
    assert ei.value.status_code == 422
    detail = str(ei.value.detail)
    assert "50" in detail or "exceeds" in detail.lower()


@pytest.mark.asyncio
async def test_send_message_rejects_total_size_over_budget(patched_send_message):
    """Five 45 MB files = 225 MB which exceeds the 200 MB total cap."""
    workspace_files = {f"uploads/big-{i}.bin": 45_000_000 for i in range(5)}
    attachments = [
        {"path": f"uploads/big-{i}.bin", "filename": f"big-{i}.bin"}
        for i in range(5)
    ]
    with pytest.raises(HTTPException) as ei:
        await patched_send_message(
            attachments=attachments,
            workspace_files=workspace_files,
        )
    assert ei.value.status_code == 422
    assert "200" in str(ei.value.detail)
    assert _MAX_ATTACHMENTS_TOTAL_BYTES == 200_000_000


@pytest.mark.asyncio
async def test_send_message_scans_attachment_filenames_for_injection(
    patched_send_message,
):
    _, _, detector = await patched_send_message(
        attachments=[
            {"path": "uploads/report.pdf", "filename": "report.pdf"},
        ],
        workspace_files={"uploads/report.pdf": 10},
    )

    # Detector should be called for content AND each filename.
    seen = {call[0] for call in detector.calls}
    assert "please summarize" in seen
    assert "report.pdf" in seen


@pytest.mark.asyncio
async def test_send_message_blocks_filename_flagged_as_injection(
    patched_send_message,
):
    """A filename containing the BLOCK_ME marker must produce 422."""
    with pytest.raises(HTTPException) as ei:
        await patched_send_message(
            attachments=[
                {
                    "path": "uploads/BLOCK_ME.pdf",
                    "filename": "BLOCK_ME.pdf",
                },
            ],
            workspace_files={"uploads/BLOCK_ME.pdf": 10},
            detector_override=_BlockingInjectionDetector(),
        )
    assert ei.value.status_code == 422
    assert "filename" in str(ei.value.detail).lower()


@pytest.mark.asyncio
async def test_send_message_omits_attachments_when_absent(patched_send_message):
    _, store, _ = await patched_send_message(
        attachments=None,
        workspace_files={},
    )
    args, _ = store.emit_event.call_args
    _sid, _type, event_data = args
    assert "attachments" not in event_data


@pytest.mark.asyncio
async def test_send_message_accepts_multiple_attachments_under_budget(
    patched_send_message,
):
    """Three 50 MB files (= 150 MB total) sit under both per-file and total caps."""
    workspace_files = {f"uploads/file-{i}.bin": 50_000_000 for i in range(3)}
    attachments = [
        {"path": f"uploads/file-{i}.bin", "filename": f"file-{i}.bin"}
        for i in range(3)
    ]
    _, store, _ = await patched_send_message(
        attachments=attachments,
        workspace_files=workspace_files,
    )
    args, _ = store.emit_event.call_args
    _sid, _type, event_data = args
    assert len(event_data["attachments"]) == 3
    assert all(a["size"] == 50_000_000 for a in event_data["attachments"])
