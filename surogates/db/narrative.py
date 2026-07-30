# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
#
"""The narrative slice of the event log, and how to search it.

Two consumers need to agree, byte for byte, on what "the searchable text
of an event" means:

* the ``idx_events_narrative_fts`` GIN index declared in
  ``observability.sql``, and
* every query that wants that index used — the ``session_search`` builtin
  here, and the control plane's cross-session search, which imports these
  helpers rather than re-deriving them.

Postgres only uses an expression index when the query's expression parses
to the same tree, and only uses a *partial* index when the query carries a
predicate the planner can prove implies the index's ``WHERE``. A drifting
copy of either does not fail loudly — it silently reverts to a sequential
scan over a table that is ~90% streamed ``llm.delta`` chunks. Hence one
module, two rendered strings, and a test that pins the ``.sql`` file
against :func:`narrative_tsvector_sql`.

Text keys, and why each is here:

``content``
    ``user.message`` prompts and ``tool.result`` payloads.
``message.content``
    ``llm.response`` assistant text. Always nested under ``message`` — a
    flat ``data->>'content'`` read returns NULL for every assistant turn,
    which is why the original search could not match the agent's own words.
``recap``
    ``turn.summary`` — the per-turn recap. The densest narrative the
    platform stores, and the corpus the report engine already summarises.
``summary``
    ``iteration.summary`` — the one-line-per-iteration fallback that keeps
    flowing when recap generation degrades.
"""
from __future__ import annotations

# Event types whose payloads carry human-readable narrative text.
#
# Deliberately excludes ``llm.delta``: deltas are persisted one row per
# streamed token chunk and carry a ``content`` key, so including them both
# dominates the index and lets per-token fragments outrank whole messages.
NARRATIVE_SEARCH_TYPES: tuple[str, ...] = (
    "user.message",
    "llm.response",
    "tool.result",
    "turn.summary",
    "iteration.summary",
)

# Event types per ``role_filter`` value accepted by the search tools.
NARRATIVE_ROLE_TYPES: dict[str, tuple[str, ...]] = {
    "user": ("user.message",),
    "assistant": ("llm.response",),
    "tool": ("tool.result",),
    "summary": ("turn.summary", "iteration.summary"),
}

# Text-search configuration for both the index and every query.
#
# ``simple`` lowercases and splits without stemming or a stopword list.
# ``english`` would stem English a little better, but it is actively wrong
# for the Romanian deployments this platform serves first: it applies
# English suffix rules to Romanian tokens. Since one shared configuration
# has to back one shared index, language-neutral exact-token matching is
# the honest choice — callers OR the morphological variants they want, and
# the tool descriptions say so.
NARRATIVE_FTS_CONFIG = "simple"

# JSONB paths concatenated into the searchable text, in index order.
_NARRATIVE_TEXT_PATHS: tuple[str, ...] = (
    "data->>'content'",
    "data->'message'->>'content'",
    "data->>'recap'",
    "data->>'summary'",
)


def narrative_text_sql(prefix: str = "") -> str:
    """Render the concatenated searchable-text expression.

    *prefix* qualifies the column reference: ``""`` for a ``CREATE INDEX``
    on ``events``, ``"e."`` inside a query that aliases the table.
    """
    parts = [f"COALESCE({prefix}{path}, '')" for path in _NARRATIVE_TEXT_PATHS]
    return " || ' ' || ".join(parts)


def narrative_tsvector_sql(prefix: str = "") -> str:
    """Render the indexed ``tsvector`` expression."""
    return f"to_tsvector('{NARRATIVE_FTS_CONFIG}', {narrative_text_sql(prefix)})"


def narrative_tsquery_sql(param: str = "query") -> str:
    """Render the query-side ``tsquery`` expression.

    ``websearch_to_tsquery`` is what makes the documented syntax real: it
    honours ``OR``, quoted phrases and leading ``-`` negation. The previous
    ``plainto_tsquery`` discarded every operator and ANDed the remaining
    lexemes, so the tool's own advice ("use OR between keywords, FTS
    defaults to AND which misses sessions") produced the exact failure it
    warned about.
    """
    return f"websearch_to_tsquery('{NARRATIVE_FTS_CONFIG}', :{param})"


def narrative_type_list_sql() -> str:
    """Render the type predicate that lets the partial index apply."""
    return ", ".join(f"'{t}'" for t in NARRATIVE_SEARCH_TYPES)
