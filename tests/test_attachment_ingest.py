import base64
from types import SimpleNamespace
from surogates.session.attachment_ingest import ingest_attachment_bytes


class _FakeStorage:
    def __init__(self): self.written = {}
    async def write(self, bucket, key, data): self.written[(bucket, key)] = data
    async def read(self, bucket, key): return self.written[(bucket, key)]


def _session():
    return SimpleNamespace(id="s1", config={"storage_key_prefix": "p/a", "storage_bucket": "b"})


async def test_image_returns_image_entry_no_storage_write():
    storage = _FakeStorage()
    out = await ingest_attachment_bytes(
        storage, session=_session(), root_id="s1", bucket="b",
        path="uploads/slack/100-0-pic.png", filename="pic.png",
        mime_type="image/png", data=b"PNGBYTES")
    assert out == {"image": {"data": base64.b64encode(b"PNGBYTES").decode(), "mime_type": "image/png"}}
    assert storage.written == {}     # images are not written to the workspace


async def test_text_doc_writes_workspace_and_returns_attachment_with_inline():
    storage = _FakeStorage()
    out = await ingest_attachment_bytes(
        storage, session=_session(), root_id="s1", bucket="b",
        path="uploads/slack/100-1-notes.txt", filename="notes.txt",
        mime_type="text/plain", data=b"hello world")
    att = out["attachment"]
    assert att["path"] == "uploads/slack/100-1-notes.txt"
    assert att["filename"] == "notes.txt" and att["mime_type"] == "text/plain"
    assert att["size"] == len(b"hello world")
    assert att.get("inlined_text") == "hello world"      # small text inlines
    # the bytes were written to the workspace key
    assert any(k[0] == "b" and "notes.txt" in k[1] for k in storage.written)


async def test_pdf_doc_materializes_bytes_and_returns_markdown(tmp_path, monkeypatch):
    from tests.tools.fixtures.build_documents import build_minimal_pdf

    # Keep the deterministic materialization path inside the test tempdir.
    import surogates.session.attachment_ingest as ingest
    monkeypatch.setattr(ingest, "_INLINE_MATERIALIZE_ROOT", tmp_path / "inline")

    pdf = build_minimal_pdf(tmp_path / "source.pdf", heading="Slack PDF")
    storage = _FakeStorage()
    out = await ingest_attachment_bytes(
        storage, session=_session(), root_id="s1", bucket="b",
        path="uploads/slack/100-2-source.pdf", filename="source.pdf",
        mime_type="application/pdf", data=pdf.read_bytes())
    att = out["attachment"]
    assert att["path"] == "uploads/slack/100-2-source.pdf"
    assert att["inlined_render_kind"] == "markdown"
    assert "Slack PDF" in att["inlined_text"]
