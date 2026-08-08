"""Tests for knowledge-base tool agent_id resolution.

The bug: ``kb_list_pages`` / ``kb_read_page`` resolved ``agent_id`` from
the ``SUROGATES_AGENT_ID`` env var, which is only set in helm mode (one
pod per agent). In the shared runtime one worker serves many agents and
that env var is unset, so KB tools failed for every shared-runtime
session with "SUROGATES_AGENT_ID is not set".

The fix resolves ``agent_id`` from the per-session tool-dispatch kwargs
(``agent_id=session.agent_id``, passed by ``harness.tool_exec``) instead
of from the process environment.
"""
from __future__ import annotations

import random
from collections import Counter

import pytest

from surogates.db import ops_engine
from surogates.db.ops_models import OpsKBWikiPage
from surogates.tools.builtin import kb_tools

AGENT_ID = "43196a20-7af0-48c0-a355-3e3a03545f66"


def test_agent_id_resolved_from_kwargs():
    """The per-session agent_id arrives in the handler kwargs."""
    assert kb_tools._agent_id_from_kwargs({"agent_id": AGENT_ID}) == AGENT_ID


def test_agent_id_missing_from_context_raises():
    """No agent_id in the dispatch context is a wiring bug -- fail loud,
    with a message that points at the context, not an operator env var."""
    with pytest.raises(RuntimeError, match="agent_id"):
        kb_tools._agent_id_from_kwargs({})


def test_agent_id_does_not_fall_back_to_env(monkeypatch):
    """Resolution must NOT read SUROGATES_AGENT_ID: in the shared runtime
    it is unset, and depending on it is exactly the bug we fixed. Even
    when the env var is present it must be ignored."""
    monkeypatch.setenv("SUROGATES_AGENT_ID", "env-agent-must-be-ignored")
    with pytest.raises(RuntimeError, match="agent_id"):
        kb_tools._agent_id_from_kwargs({})


async def test_kb_list_pages_fails_closed_without_agent_id(monkeypatch):
    """The handler resolves agent_id from its kwargs and fails closed
    when the dispatch context carries none -- never silently reading the
    env var instead."""
    monkeypatch.setattr(ops_engine, "_session_factory", None)
    monkeypatch.delenv("SUROGATES_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="agent_id"):
        await kb_tools._kb_list_pages_handler({"kb_id": "some-kb"})


async def test_kb_read_page_fails_closed_without_agent_id(monkeypatch):
    """Same contract for kb_read_page."""
    monkeypatch.setattr(ops_engine, "_session_factory", None)
    monkeypatch.delenv("SUROGATES_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="agent_id"):
        await kb_tools._kb_read_page_handler(
            {"kb_id": "some-kb", "path": "index.md"},
        )


def test_agent_knowledge_bases_read_model_has_mode_column():
    """The read-side M2M mirrors the writer-side mode column so the
    worker can SELECT it when loading attached KBs."""
    from surogates.db.ops_models import agent_knowledge_bases

    assert "mode" in agent_knowledge_bases.c


def _page(path: str, page_type: str) -> OpsKBWikiPage:
    """A detached wiki-page row. _select_tree_pages only reads path and
    page_type, so no session and no real KB is needed."""
    return OpsKBWikiPage(
        id=path, kb_id="kb-1", path=path, page_type=page_type,
        title=path.rsplit("/", 1)[-1], size_bytes=1024,
    )


def _prod_shaped_kb() -> list[OpsKBWikiPage]:
    """The shape of the PROD KB that exposed the bug: 4572 pages whose
    'concepts/' paths all sort before 'index.md' and 'summaries/'."""
    return (
        [_page("index.md", "index")]
        + [_page(f"concepts/c{i:04d}.md", "concept") for i in range(625)]
        + [_page(f"summaries/s{i:04d}.md", "summary") for i in range(3946)]
    )


def test_tree_selection_keeps_every_page_type_represented():
    pages = (
        [_page(f"concepts/c{i}.md", "concept") for i in range(625)]
        + [_page("index.md", "index")]
        + [_page(f"summaries/s{i}.md", "summary") for i in range(3946)]
    )
    picked = kb_tools._select_tree_pages(pages, budget=200)
    kinds = {p.page_type for p in picked}
    assert "index" in kinds, "the entry point must never be cut"
    assert "summary" in kinds, "path-order truncation hid every summary"
    assert "concept" in kinds
    assert len(picked) <= 200


def test_select_tree_pages_keeps_every_type_represented():
    """The bug: a path-ordered slice taken before the group-by handed the
    agent 200 concept pages, ZERO summaries and no index."""
    selected = kb_tools._select_tree_pages(_prod_shaped_kb(), budget=200)

    by_type = Counter(p.page_type for p in selected)
    assert len(selected) == 200
    assert by_type["index"] == 1
    assert by_type["summary"] > 0
    assert by_type["concept"] > 0
    # Equal share of the 199 non-index slots, off by at most the odd page.
    assert abs(by_type["summary"] - by_type["concept"]) <= 1


def test_select_tree_pages_never_cuts_index_pages():
    """Index pages are the entry point and are always few -- they come off
    the top of the budget, never out of a type's share."""
    pages = (
        [_page(f"index-{i}.md", "index") for i in range(5)]
        + [_page(f"summaries/s{i:04d}.md", "summary") for i in range(1000)]
    )
    selected = kb_tools._select_tree_pages(pages, budget=200)

    assert sum(1 for p in selected if p.page_type == "index") == 5
    assert len(selected) == 200


def test_select_tree_pages_excludes_source_pages():
    """'source' rows are raw document dumps: never in the ToC, still
    readable with kb_read_page."""
    pages = (
        [_page(f"sources/d{i:03d}.md", "source") for i in range(300)]
        + [_page(f"summaries/s{i:03d}.md", "summary") for i in range(10)]
    )
    selected = kb_tools._select_tree_pages(pages, budget=200)

    assert [p.page_type for p in selected] == ["summary"] * 10
    assert not any(p.path.startswith("sources/") for p in selected)


def test_select_tree_pages_drops_a_source_only_kb_to_empty():
    """Nothing but sources means nothing to navigate -- an empty list, not
    a crash on the zip_longest with no buckets."""
    pages = [_page(f"sources/d{i}.md", "source") for i in range(20)]

    assert kb_tools._select_tree_pages(pages, budget=200) == []
    assert kb_tools._select_tree_pages([], budget=200) == []


def test_select_tree_pages_redistributes_an_unused_share():
    """A type smaller than its equal share is taken whole and its leftover
    quota flows to the bigger types instead of being wasted."""
    pages = (
        [_page("index.md", "index")]
        + [_page(f"concepts/c{i}.md", "concept") for i in range(3)]
        + [_page(f"summaries/s{i:04d}.md", "summary") for i in range(500)]
    )
    selected = kb_tools._select_tree_pages(pages, budget=200)

    by_type = Counter(p.page_type for p in selected)
    assert len(selected) == 200
    assert by_type == {"summary": 196, "concept": 3, "index": 1}


def test_select_tree_pages_under_budget_returns_everything_but_sources():
    """No cut when the KB fits -- the caller's 'showing N of M' note keys
    off this and must not fire for a small KB."""
    pages = [_page("index.md", "index")] + [
        _page(f"summaries/s{i}.md", "summary") for i in range(20)
    ]

    assert len(kb_tools._select_tree_pages(pages, budget=200)) == 21


def test_select_tree_pages_is_deterministic_and_path_ordered():
    """The tree is injected into the system prompt on every wake: the same
    KB must yield the same bytes regardless of row order from the DB, and
    the kept pages must be the lowest paths in each type."""
    pages = _prod_shaped_kb()
    shuffled = list(pages)
    random.Random(1234).shuffle(shuffled)

    first = kb_tools._select_tree_pages(pages, budget=200)
    second = kb_tools._select_tree_pages(shuffled, budget=200)

    assert [p.path for p in first] == [p.path for p in second]
    concepts = [p.path for p in first if p.page_type == "concept"]
    assert concepts == sorted(concepts)
    assert concepts[0] == "concepts/c0000.md"


def test_select_tree_pages_never_exceeds_budget():
    """Hard cap, including the degenerate budgets."""
    pages = _prod_shaped_kb()

    assert len(kb_tools._select_tree_pages(pages, budget=1)) == 1
    assert len(kb_tools._select_tree_pages(pages, budget=7)) == 7
    assert len(kb_tools._select_tree_pages(pages, budget=200)) == 200
