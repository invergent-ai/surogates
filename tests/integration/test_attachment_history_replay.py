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

    real_parser = file_ops._parse_document_to_text

    async def counting(p):
        call_count["n"] += 1
        return await real_parser(p)

    monkeypatch.setattr(file_ops, "_parse_document_to_text", counting)

    third = AgentHarness._rebuild_messages(SimpleNamespace(), [event])
    assert third == first
    assert call_count["n"] == 0, (
        "replay should not re-invoke the parser"
    )
