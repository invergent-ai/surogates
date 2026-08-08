# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the kb_search_pages builtin tool.

Three things must hold and are cheap to break:

1. Routing. A builtin missing from ``TOOL_LOCATIONS`` falls through to
   the ``SANDBOX`` default and surfaces to the LLM as "Unknown tool"
   from a pod with no DB access -- the handler never runs.
2. Scope. Search must see exactly the KBs ``kb_list_pages`` /
   ``kb_read_page`` would allow: attached to this agent AND included in
   the sender's pinned package.
3. The tsvector configuration. The query config must match the ops-side
   generated column ('simple', not 'english') or nothing ever matches.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.tools.builtin import kb_tools
from surogates.tools.registry import ToolRegistry
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

AGENT_ID = "43196a20-7af0-48c0-a355-3e3a03545f66"
KB_A = "11111111-1111-1111-1111-111111111111"
KB_B = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------
# Fakes -- the handler only needs an object with ``execute``.
# ---------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows


class _FakeSession:
    """Returns the queued result sets in order and records every call."""

    def __init__(self, *result_sets):
        self._queued = list(result_sets)
        self.executed = []

    async def execute(self, statement, params=None):
        self.executed.append((statement, params))
        rows = self._queued.pop(0) if self._queued else []
        return _FakeResult(rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _hit(kb_id=KB_A, path="sources/d1.md", title="Photosynthesis"):
    return SimpleNamespace(
        kb_id=kb_id,
        path=path,
        page_type="summary",
        title=title,
        brief="How plants convert light into sugar.",
        snippet="chlorophyll absorbs **light** in the thylakoid",
        rank=0.42,
    )


def _use_session(monkeypatch, session):
    monkeypatch.setattr(
        kb_tools, "ensure_ops_session_factory", lambda: (lambda: session),
    )


# ---------------------------------------------------------------------
# 1. Routing regression
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_name", ["kb_list_pages", "kb_read_page", "kb_search_pages"],
)
def test_kb_tools_route_to_harness(tool_name: str) -> None:
    assert TOOL_LOCATIONS.get(tool_name) is ToolLocation.HARNESS, (
        f"{tool_name} must have an explicit HARNESS entry in "
        "TOOL_LOCATIONS; the default SANDBOX fallback routes the call "
        "to a sandbox pod with no ops-DB access, which answers "
        "'Unknown tool' without ever running the handler."
    )


def test_resolve_location_for_kb_search() -> None:
    """End-to-end through the public resolver, not just the dict."""
    from unittest.mock import MagicMock

    from surogates.tools.router import ToolRouter

    router = ToolRouter(
        registry=MagicMock(),
        sandbox_pool=MagicMock(),
        governance=MagicMock(),
    )
    assert router.resolve_location("kb_search_pages") is (
        ToolLocation.HARNESS
    )


def test_registered_in_the_knowledge_toolset() -> None:
    registry = ToolRegistry()
    kb_tools.register(registry)

    entry = registry.get("kb_search_pages")
    assert entry is not None
    assert entry.toolset == "knowledge"
    assert entry.handler is kb_tools._kb_search_pages_handler
    assert entry.schema.parameters["required"] == ["query"]


def test_description_states_the_retrieval_order() -> None:
    """The description is the only place the model learns that the
    injected tree is partial and that search comes before reading."""
    registry = ToolRegistry()
    kb_tools.register(registry)

    description = registry.get("kb_search_pages").schema.description
    assert "FIRST" in description
    assert "truncated" in description
    assert "kb_read_page" in description


# ---------------------------------------------------------------------
# 2. Scope
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scope_is_the_agent_attachment_set() -> None:
    session = _FakeSession([(KB_A, "Biology"), (KB_B, "Chemistry")])

    kbs = await kb_tools._searchable_kbs(
        session, agent_id=AGENT_ID, kwargs={},
    )

    assert kbs == [(KB_A, "Biology"), (KB_B, "Chemistry")]


@pytest.mark.asyncio
async def test_scope_drops_kbs_outside_the_pinned_package() -> None:
    """Same allowlist kb_read_page enforces per call: a KB attached to
    the agent but excluded from this sender's package is not searched."""
    session = _FakeSession([(KB_A, "Biology"), (KB_B, "Chemistry")])

    kbs = await kb_tools._searchable_kbs(
        session,
        agent_id=AGENT_ID,
        kwargs={"session_config": {"entitlements": {"kb_ids": [KB_B]}}},
    )

    assert kbs == [(KB_B, "Chemistry")]


@pytest.mark.asyncio
async def test_named_kb_outside_scope_never_reaches_the_query(
    monkeypatch,
) -> None:
    """An injected kb_id must not widen the search: the handler refuses
    before issuing the search query at all."""
    session = _FakeSession([(KB_A, "Biology")])
    _use_session(monkeypatch, session)

    out = await kb_tools._kb_search_pages_handler(
        {"query": "photosynthesis", "kb": KB_B}, agent_id=AGENT_ID,
    )

    assert KB_B in out and out.startswith("Error:")
    assert "Biology" in out  # tells the model what it may search
    assert len(session.executed) == 1  # scope lookup only, no search


@pytest.mark.asyncio
async def test_no_attached_kbs_returns_an_error(monkeypatch) -> None:
    session = _FakeSession([])
    _use_session(monkeypatch, session)

    out = await kb_tools._kb_search_pages_handler(
        {"query": "anything"}, agent_id=AGENT_ID,
    )

    assert out.startswith("Error:")
    assert len(session.executed) == 1


@pytest.mark.asyncio
async def test_search_without_kb_spans_every_attached_kb(
    monkeypatch,
) -> None:
    session = _FakeSession(
        [(KB_A, "Biology"), (KB_B, "Chemistry")],
        [_hit(kb_id=KB_B, path="concepts/redox.md", title="Redox")],
    )
    _use_session(monkeypatch, session)

    out = await kb_tools._kb_search_pages_handler(
        {"query": "electron transfer"}, agent_id=AGENT_ID,
    )

    _, params = session.executed[1]
    assert params["kb_ids"] == [KB_A, KB_B]
    assert params["query"] == "electron transfer"
    assert params["limit"] == kb_tools._SEARCH_LIMIT_DEFAULT
    assert "concepts/redox.md" in out
    assert KB_B in out  # kb_read_page needs the id, so it must be shown


@pytest.mark.asyncio
async def test_kb_can_be_named_by_display_name(monkeypatch) -> None:
    session = _FakeSession(
        [(KB_A, "Biology"), (KB_B, "Chemistry")], [_hit()],
    )
    _use_session(monkeypatch, session)

    await kb_tools._kb_search_pages_handler(
        {"query": "light", "kb": "biology"}, agent_id=AGENT_ID,
    )

    _, params = session.executed[1]
    assert params["kb_ids"] == [KB_A]


@pytest.mark.asyncio
async def test_fails_closed_without_agent_id(monkeypatch) -> None:
    """agent_id comes from the dispatch kwargs, never from
    SUROGATES_AGENT_ID -- that env var is unset in the shared runtime."""
    monkeypatch.setenv("SUROGATES_AGENT_ID", "env-agent-must-be-ignored")
    with pytest.raises(RuntimeError, match="agent_id"):
        await kb_tools._kb_search_pages_handler({"query": "anything"})


@pytest.mark.asyncio
async def test_empty_query_is_rejected() -> None:
    out = await kb_tools._kb_search_pages_handler(
        {"query": "   "}, agent_id=AGENT_ID,
    )
    assert out == "Error: query is required."


# ---------------------------------------------------------------------
# 3. Query shape + rendering
# ---------------------------------------------------------------------

def test_search_sql_matches_the_ops_tsvector_configuration() -> None:
    """'simple' must match the generated column's configuration, and the
    NOT NULL predicate must match the partial index or PG seq-scans."""
    sql = kb_tools._SEARCH_SQL.text

    assert "websearch_to_tsquery('simple', :query)" in sql
    assert "ts_headline('simple'" in sql
    assert "english" not in sql
    assert "p.content IS NOT NULL" in sql
    assert "p.search_tsv @@ q.tsq" in sql


def test_headline_runs_after_the_limit() -> None:
    """Headlining before LIMIT would run over every matching page."""
    sql = kb_tools._SEARCH_SQL.text
    assert sql.index("LIMIT :limit") < sql.index("ts_headline")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, kb_tools._SEARCH_LIMIT_DEFAULT),
        ("seven", kb_tools._SEARCH_LIMIT_DEFAULT),
        ("3", 3),
        (0, 1),
        (-5, 1),
        (10_000, kb_tools._SEARCH_LIMIT_MAX),
    ],
)
def test_limit_is_clamped(raw, expected) -> None:
    assert kb_tools._clamp_limit(raw) == expected


def test_hits_render_path_title_and_snippet() -> None:
    out = kb_tools._format_search_hits(
        [_hit()], names={KB_A: "Biology"}, query="light",
    )

    assert "`sources/d1.md`" in out
    assert "Photosynthesis" in out
    assert "chlorophyll absorbs **light**" in out
    assert KB_A in out
    assert "kb_read_page" in out


def test_no_hits_points_at_the_recovery_path() -> None:
    out = kb_tools._format_search_hits(
        [], names={}, query="quantum badgers",
    )

    assert "quantum badgers" in out
    assert "kb_list_pages" in out
