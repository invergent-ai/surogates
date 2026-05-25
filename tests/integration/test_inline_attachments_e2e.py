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
