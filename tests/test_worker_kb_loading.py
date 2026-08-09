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
    """The existing degrade-gracefully contract is preserved for a
    genuine connection failure (the KB-list query itself can't run)."""
    monkeypatch.setattr(ops_engine, "_session_factory", None)
    kbs = await _load_attached_kbs(
        agent_id=AGENT_ID, ops_db_url="sqlite+aiosqlite://",
    )
    assert kbs == []


class _FailingPagesSession:
    """Wraps a real AsyncSession: the first execute() (the KB-list
    query) passes through untouched; every execute() after that (the
    page-tree query) raises. Simulates an ops DB that hasn't run the
    migration adding ``OpsKBWikiPage.brief`` yet -- the KB-list query
    doesn't touch that column, only the page-tree query does.
    """

    def __init__(self, real):
        self._real = real
        self._calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        self._calls += 1
        if self._calls > 1:
            from sqlalchemy.exc import ProgrammingError

            raise ProgrammingError(
                "column kb_wiki_pages.brief does not exist", None, None,
            )
        return await self._real.execute(*a, **k)

    async def rollback(self):
        await self._real.rollback()


async def test_page_tree_failure_keeps_kb_names_and_tools_alive(
    seeded_ops_factory, monkeypatch,
):
    """A page-tree-query-specific failure must not wipe the whole KB
    list. kb_search_pages calls the ops HTTP endpoint, not this table,
    and kb_list_pages/kb_read_page each re-read this table per call
    regardless -- silently dropping every KB tool over a failure only
    the tree query hit was strictly worse than serving names with no
    tree. Losing the tree should cost only the tree."""
    real_factory = ops_engine.get_ops_session_factory()

    def failing_factory():
        return _FailingPagesSession(real_factory())

    monkeypatch.setattr(ops_engine, "_session_factory", failing_factory)

    kbs = await _load_attached_kbs(
        agent_id=AGENT_ID, ops_db_url="sqlite+aiosqlite://",
    )

    # Both attachments survive -- this is what worker.py checks with
    # `if not attached_kbs:` to decide whether to drop every KB tool.
    assert len(kbs) == 2
    by_id = {kb["id"]: kb for kb in kbs}
    assert by_id[KB_GROUNDING]["display_name"] == "Platform Docs"
    assert by_id[KB_GROUNDING]["mode"] == "grounding"
    assert by_id[KB_REFERENCE]["display_name"] == "Extra Notes"
    # The tree is genuinely absent, not a false "(empty)" note -- those
    # mean different things (query failed vs. KB really has no pages).
    assert "pages_tree" not in by_id[KB_GROUNDING]
    assert "pages_total" not in by_id[KB_GROUNDING]


async def test_load_attached_kbs_tree_spans_every_page_type(
    seeded_ops_factory,
):
    """End-to-end shape of the PROD bug: a KB whose 'concepts/' paths sort
    before 'index.md' and 'summaries/' must still show all three in the
    injected tree, with sources excluded and an accurate cut note."""
    async with seeded_ops_factory() as s:
        for i in range(250):
            s.add(OpsKBWikiPage(
                id=f"sum-{i}", kb_id=KB_REFERENCE,
                path=f"summaries/s{i:03d}.md", page_type="summary",
                title=f"Summary {i}", size_bytes=512,
            ))
        for i in range(60):
            s.add(OpsKBWikiPage(
                id=f"con-{i}", kb_id=KB_REFERENCE,
                path=f"concepts/c{i:03d}.md", page_type="concept",
                title=f"Concept {i}", size_bytes=2048,
            ))
        for i in range(30):
            s.add(OpsKBWikiPage(
                id=f"src-{i}", kb_id=KB_REFERENCE,
                path=f"sources/d{i:03d}.md", page_type="source",
                title=f"Doc {i}", size_bytes=99_000,
            ))
        s.add(OpsKBWikiPage(
            id="idx", kb_id=KB_REFERENCE, path="index.md",
            page_type="index", title="Index", size_bytes=900,
        ))
        await s.commit()

    kbs = await _load_attached_kbs(
        agent_id=AGENT_ID, ops_db_url="sqlite+aiosqlite://",
    )
    tree = next(kb for kb in kbs if kb["id"] == KB_REFERENCE)["pages_tree"]

    assert "## index" in tree
    assert "## summary" in tree
    assert "## concept" in tree
    assert "index.md" in tree
    # Raw document dumps stay out of the prompt (kb_read_page reaches them).
    assert "sources/" not in tree
    assert "## source" not in tree
    # 341 rows total, 200 shown -- the note counts every page the agent can
    # still reach via kb_list_pages, including the excluded sources.
    assert "(showing 200 of 341 pages" in tree
    assert len([ln for ln in tree.splitlines() if ln.startswith("- `")]) == 200
