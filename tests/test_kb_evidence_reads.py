"""Read exact published passage evidence through the real DB authorization lookup."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.db.ops_models import (
    OpsKnowledgeBase,
    OpsKBWikiPage,
    agent_knowledge_bases,
)
from surogates.tools.builtin import kb_tools


@pytest.fixture
async def evidence_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            OpsKnowledgeBase.__table__,
            OpsKBWikiPage.__table__,
            agent_knowledge_bases,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            OpsKnowledgeBase(
                id="kb",
                project_id="project",
                name="kb",
                display_name="KB",
                description="",
                status="active",
                hub_ref="test/kb",
            )
        )
        await s.execute(
            agent_knowledge_bases.insert().values(
                agent_id="agent", kb_id="kb", mode="authoritative"
            )
        )
        await s.commit()
    monkeypatch.setattr(kb_tools, "ensure_ops_session_factory", lambda: factory)
    monkeypatch.setattr(
        "surogates.config.Settings",
        lambda: SimpleNamespace(
            kb_hub=SimpleNamespace(
                endpoint_url="https://unused.invalid",
                access_key_id="test",
                secret_access_key="test",
            )
        ),
    )
    yield factory
    await engine.dispose()


async def add_artifact(factory, raw, *, pdf=False, kb_id="kb"):
    path = "sources/manual.json" if pdf else "sources/manual.md"
    digest = hashlib.sha256(raw).hexdigest()
    async with factory() as s:
        s.add(
            OpsKBWikiPage(
                id="parent",
                kb_id=kb_id,
                path=path,
                title="Manual",
                page_type="source",
                size_bytes=len(raw),
                hub_object_path="wiki/.versions/published/manual",
                content_sha256=digest,
            )
        )
        s.add(
            OpsKBWikiPage(
                id="passage",
                kb_id=kb_id,
                path="_passages/manual.md",
                parent_path=path,
                title="Manual",
                page_type="source",
                size_bytes=12,
                hub_object_path="wiki/.versions/published/manual",
                content_sha256=digest,
                source_start=6,
                source_end=18,
                page_number=27 if pdf else None,
            )
        )
        await s.commit()
    return path


async def test_markdown_passage_uses_pinned_object_and_exact_offsets(
    evidence_db, monkeypatch
):
    raw = b"prefixExact clause suffix"
    path = await add_artifact(evidence_db, raw)
    fetch = AsyncMock(return_value=raw)
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "passage_id": "passage"}, agent_id="agent"
    )
    assert out.endswith("Exact clause")
    assert "characters=6-18" in out and "sha256=" in out
    assert fetch.call_args.kwargs["path"] == "wiki/.versions/published/manual"


async def test_pdf_passage_and_page_range_are_readable(evidence_db, monkeypatch):
    raw = json.dumps([{"page": 27, "content": "prefixExact clause suffix"}]).encode()
    path = await add_artifact(evidence_db, raw, pdf=True)
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "passage_id": "passage"}, agent_id="agent"
    )
    assert out.endswith("Exact clause") and "page=27" in out
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "pages": "27"}, agent_id="agent"
    )
    assert "Exact clause" in out and "Page 27" in out


async def test_pdf_range_obeys_character_budget(evidence_db, monkeypatch):
    raw = json.dumps([{"page": 27, "content": "x" * 200000}]).encode()
    path = await add_artifact(evidence_db, raw, pdf=True)
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", AsyncMock(return_value=raw))
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "pages": "27", "limit": 1000}, agent_id="agent"
    )
    assert len(out) < 1500 and "Continue with offset=1000" in out


async def test_hash_mismatch_fails_closed(evidence_db, monkeypatch):
    path = await add_artifact(evidence_db, b"expected evidence")
    monkeypatch.setattr(
        kb_tools, "fetch_wiki_object", AsyncMock(return_value=b"wrong version")
    )
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path}, agent_id="agent"
    )
    assert out.startswith("Error: published artifact hash mismatch")


async def test_passage_id_cannot_cross_kb_boundary(evidence_db, monkeypatch):
    path = await add_artifact(evidence_db, b"private evidence", kb_id="private")
    fetch = AsyncMock()
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "passage_id": "passage"}, agent_id="agent"
    )
    assert "not found" in out
    fetch.assert_not_called()


async def test_detached_agent_cannot_read_passage(evidence_db, monkeypatch):
    path = await add_artifact(evidence_db, b"prefixExact clause")
    fetch = AsyncMock()
    monkeypatch.setattr(kb_tools, "fetch_wiki_object", fetch)
    out = await kb_tools._kb_read_page_handler(
        {"kb_id": "kb", "path": path, "passage_id": "passage"}, agent_id="other"
    )
    assert "not attached" in out
    fetch.assert_not_called()
