"""Tests for _load_attached_kbs: per-attachment mode + page-tree loading.

Uses an in-memory SQLite stand-in for the ops DB (the read-side models
are plain String/Text/Integer, so SQLite is schema-compatible) wired in
via the ops_engine module-level factory that _load_attached_kbs reads.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from surogates.db import ops_engine
from surogates.db.ops_models import (
    OpsBase,
    OpsKBWikiPage,
    OpsKnowledgeBase,
    agent_knowledge_bases,
)
from surogates.orchestrator.worker import _load_attached_kbs

AGENT_ID = "agent-1"
KB_GROUNDING = "kb-grounding"
KB_REFERENCE = "kb-reference"


@pytest.fixture
async def seeded_ops_factory(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(OpsBase.metadata.create_all)

    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )
    async with factory() as s:
        s.add(OpsKnowledgeBase(
            id=KB_GROUNDING, project_id="p1", name="platform-docs",
            display_name="Platform Docs", description="What Surogate does",
            status="active", hub_ref="repo-a",
        ))
        s.add(OpsKnowledgeBase(
            id=KB_REFERENCE, project_id="p1", name="extra-notes",
            display_name="Extra Notes", description="Optional notes",
            status="active", hub_ref="repo-b",
        ))
        s.add(OpsKBWikiPage(
            id="pg-1", kb_id=KB_GROUNDING, path="index.md",
            page_type="index", title="Index", size_bytes=1024,
        ))
        s.add(OpsKBWikiPage(
            id="pg-2", kb_id=KB_GROUNDING, path="concepts/training.md",
            page_type="concept", title="Training Methods", size_bytes=2048,
        ))
        await s.execute(agent_knowledge_bases.insert().values(
            agent_id=AGENT_ID, kb_id=KB_GROUNDING, mode="grounding",
        ))
        await s.execute(agent_knowledge_bases.insert().values(
            agent_id=AGENT_ID, kb_id=KB_REFERENCE, mode="reference",
        ))
        await s.commit()

    monkeypatch.setattr(ops_engine, "_session_factory", factory)
    yield factory
    await engine.dispose()


async def test_load_attached_kbs_includes_mode(seeded_ops_factory):
    kbs = await _load_attached_kbs(
        agent_id=AGENT_ID, ops_db_url="sqlite+aiosqlite://",
    )
    by_id = {kb["id"]: kb for kb in kbs}
    assert by_id[KB_GROUNDING]["mode"] == "grounding"
    assert by_id[KB_REFERENCE]["mode"] == "reference"


async def test_load_attached_kbs_includes_page_tree(seeded_ops_factory):
    kbs = await _load_attached_kbs(
        agent_id=AGENT_ID, ops_db_url="sqlite+aiosqlite://",
    )
    grounding = next(kb for kb in kbs if kb["id"] == KB_GROUNDING)
    assert grounding["pages_total"] == 2
    assert "index.md" in grounding["pages_tree"]
    assert "concepts/training.md" in grounding["pages_tree"]
    assert "Training Methods" in grounding["pages_tree"]


async def test_load_attached_kbs_empty_kb_has_empty_tree_note(
    seeded_ops_factory,
):
    kbs = await _load_attached_kbs(
        agent_id=AGENT_ID, ops_db_url="sqlite+aiosqlite://",
    )
    reference = next(kb for kb in kbs if kb["id"] == KB_REFERENCE)
    assert reference["pages_total"] == 0
    assert "empty" in reference["pages_tree"]


async def test_load_attached_kbs_caps_tree_at_200_pages(
    seeded_ops_factory,
):
    """A pathological KB cannot flood the prompt; the cap is announced."""
    async with seeded_ops_factory() as s:
        for i in range(250):
            s.add(OpsKBWikiPage(
                id=f"bulk-{i}", kb_id=KB_REFERENCE,
                path=f"sources/d{i:03d}.md", page_type="summary",
                title=f"Doc {i}", size_bytes=512,
            ))
        await s.commit()

    kbs = await _load_attached_kbs(
        agent_id=AGENT_ID, ops_db_url="sqlite+aiosqlite://",
    )
    reference = next(kb for kb in kbs if kb["id"] == KB_REFERENCE)
    assert reference["pages_total"] == 250
    assert "showing 200 of 250 pages" in reference["pages_tree"]
    assert "sources/d000.md" in reference["pages_tree"]


async def test_load_attached_kbs_failure_still_degrades_to_empty(
    monkeypatch,
):
    """The existing degrade-gracefully contract is preserved."""
    monkeypatch.setattr(ops_engine, "_session_factory", None)
    kbs = await _load_attached_kbs(
        agent_id=AGENT_ID, ops_db_url="sqlite+aiosqlite://",
    )
    assert kbs == []
