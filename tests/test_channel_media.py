"""Tests for the outbound MEDIA: marker helper.

Covers marker parse/strip, workspace-path normalization (incl. traversal
rejection), and reading workspace bytes via a mocked storage backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from surogates.channels.channel_media import (
    OutboundFile,
    normalize_workspace_path,
    parse_media_markers,
    resolve_workspace_media,
)


class TestParseMediaMarkers:
    def test_no_marker_returns_text_unchanged(self):
        assert parse_media_markers("hello world") == ([], "hello world")

    def test_single_marker_extracted_and_stripped(self):
        paths, cleaned = parse_media_markers(
            "The PDF is built. Sharing it here. MEDIA:/workspace/report.pdf"
        )
        assert paths == ["/workspace/report.pdf"]
        assert cleaned == "The PDF is built. Sharing it here."

    def test_multiple_markers_extracted_in_order(self):
        paths, cleaned = parse_media_markers(
            "a MEDIA:/workspace/a.png and b MEDIA:/workspace/b.png done"
        )
        assert paths == ["/workspace/a.png", "/workspace/b.png"]
        assert cleaned == "a and b done"

    def test_backtick_and_quote_wrapping_stripped(self):
        paths, cleaned = parse_media_markers("see `MEDIA:/workspace/x.pdf`")
        assert paths == ["/workspace/x.pdf"]
        assert cleaned == "see"

    def test_marker_only_text_becomes_empty(self):
        paths, cleaned = parse_media_markers("MEDIA:/workspace/only.pdf")
        assert paths == ["/workspace/only.pdf"]
        assert cleaned == ""


class TestNormalizeWorkspacePath:
    def test_workspace_prefixed_variants_all_normalize(self):
        assert normalize_workspace_path("/workspace/x.pdf") == "x.pdf"
        assert normalize_workspace_path("workspace/x.pdf") == "x.pdf"
        assert normalize_workspace_path("/x.pdf") == "x.pdf"
        assert normalize_workspace_path("x.pdf") == "x.pdf"

    def test_nested_path_preserved(self):
        assert normalize_workspace_path("/workspace/media/a.png") == "media/a.png"

    def test_traversal_and_empty_rejected(self):
        assert normalize_workspace_path("../etc/passwd") is None
        assert normalize_workspace_path("/workspace/../x") is None
        assert normalize_workspace_path("workspace/..") is None
        assert normalize_workspace_path("") is None
        assert normalize_workspace_path("/") is None


@dataclass
class _FakeSession:
    id: str = "11111111-1111-1111-1111-111111111111"
    config: dict = field(default_factory=lambda: {"storage_bucket": "wsbucket"})


class _FakeStorage:
    """Mocked storage backend: a {key: bytes} map under a single bucket."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self._objects = objects or {}
        self.reads: list[tuple[str, str]] = []

    async def stat(self, bucket: str, key: str) -> dict[str, Any]:
        if key not in self._objects:
            raise KeyError(f"{bucket}/{key}")
        return {"size": len(self._objects[key])}

    async def read(self, bucket: str, key: str) -> bytes:
        self.reads.append((bucket, key))
        if key not in self._objects:
            raise KeyError(f"{bucket}/{key}")
        return self._objects[key]


def _key(rel: str) -> str:
    from surogates.storage.tenant import prefixed_session_workspace_key
    from surogates.session.attachment_ingest import workspace_root_id
    session = _FakeSession()
    return prefixed_session_workspace_key(session.config, workspace_root_id(session), rel)


class TestResolveWorkspaceMedia:
    async def test_reads_valid_file(self):
        session = _FakeSession()
        storage = _FakeStorage({_key("report.pdf"): b"%PDF-1.4 data"})
        files = await resolve_workspace_media(
            storage, session, paths=["/workspace/report.pdf"], max_files=10, max_bytes=1024,
        )
        assert len(files) == 1
        assert files[0].filename == "report.pdf"
        assert files[0].mime_type == "application/pdf"
        assert files[0].data == b"%PDF-1.4 data"

    async def test_missing_file_skipped(self):
        session = _FakeSession()
        storage = _FakeStorage({})
        files = await resolve_workspace_media(
            storage, session, paths=["/workspace/nope.pdf"], max_files=10, max_bytes=1024,
        )
        assert files == []

    async def test_over_cap_skipped_without_read(self):
        session = _FakeSession()
        storage = _FakeStorage({_key("big.bin"): b"x" * 100})
        files = await resolve_workspace_media(
            storage, session, paths=["/workspace/big.bin"], max_files=10, max_bytes=10,
        )
        assert files == []
        assert storage.reads == []  # cap enforced on stat, before read

    async def test_traversal_path_skipped(self):
        session = _FakeSession()
        storage = _FakeStorage({})
        files = await resolve_workspace_media(
            storage, session, paths=["/workspace/../escape"], max_files=10, max_bytes=1024,
        )
        assert files == []

    async def test_max_files_caps_iteration(self):
        session = _FakeSession()
        storage = _FakeStorage({
            _key("a.png"): b"a", _key("b.png"): b"b", _key("c.png"): b"c",
        })
        files = await resolve_workspace_media(
            storage, session,
            paths=["/workspace/a.png", "/workspace/b.png", "/workspace/c.png"],
            max_files=2, max_bytes=1024,
        )
        assert [f.filename for f in files] == ["a.png", "b.png"]

    async def test_empty_bucket_yields_nothing(self):
        session = _FakeSession(config={"storage_bucket": ""})
        storage = _FakeStorage({_key("report.pdf"): b"data"})
        files = await resolve_workspace_media(
            storage, session, paths=["/workspace/report.pdf"], max_files=10, max_bytes=1024,
        )
        assert files == []
