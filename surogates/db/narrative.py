# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
#
"""The narrative slice of the event log, and how to search it.

Two consumers need to agree, byte for byte, on what "the searchable text
of an event" means:

* the ``idx_events_narrative_fts_v2`` GIN index declared in
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

One text key is worth calling out: ``llm.response`` assistant text is
always nested under ``message``, so a flat ``data->>'content'`` read
returns NULL for every assistant turn — which is why the original search
could not match the agent's own words.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

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

# Hard ceiling on the text handed to ``to_tsvector``.
#
# A tsvector may hold at most 1,048,575 bytes of lexeme data, and exceeding
# it is an ERROR, not a truncation. ``user.message`` bodies are unbounded —
# the API takes ``content: str`` with no ``max_length`` — so without this cap
# a single very long message has two failure modes, both on paths that must
# not fail: the INSERT of that event is rejected once the index exists
# (breaking the hot append path for a live session), and ``CREATE INDEX``
# aborts if such a row already exists, which rolls back the whole
# ``observability.sql`` transaction and fails the migration.
#
# 500k characters is far past any real message and leaves room for the
# worst realistic lexeme-storage ratio, where every token is distinct.
# Search quality is unaffected: nothing near that length is a conversation.
NARRATIVE_TEXT_LIMIT = 500_000


def narrative_text_sql(prefix: str = "") -> str:
    """Render the concatenated searchable-text expression.

    *prefix* qualifies the column reference: ``""`` for a ``CREATE INDEX``
    on ``events``, ``"e."`` inside a query that aliases the table.

    The result is capped at :data:`NARRATIVE_TEXT_LIMIT` — see there for why
    an uncapped expression can refuse writes and abort migrations.
    """
    parts = [f"COALESCE({prefix}{path}, '')" for path in _NARRATIVE_TEXT_PATHS]
    joined = " || ' ' || ".join(parts)
    return f"left({joined}, {NARRATIVE_TEXT_LIMIT})"


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


def narrative_type_list_sql(
    types: Sequence[str] = NARRATIVE_SEARCH_TYPES,
) -> str:
    """Render the ``IN`` list of event types for a query's type predicate.

    Spelled as literals rather than a bound array because the planner has to
    *see* the values to prove they imply the partial index's ``WHERE``; a
    bound ``= ANY(:types)`` leaves it unable to, and the index goes unused.
    *types* is always a subset of :data:`NARRATIVE_SEARCH_TYPES` chosen
    server-side, never caller text.
    """
    return ", ".join(f"'{t}'" for t in types)


def narrative_types_for_roles(roles: Optional[Iterable[str]]) -> tuple[str, ...]:
    """Map caller role names to the event types they select.

    Shared because both search implementations accept the same ``role``
    vocabulary, and a role added on one side but not the other is a silent
    difference in what each will find.

    Raises :class:`ValueError` naming the unknown roles; callers render that
    however their surface requires. An empty or all-blank selection means
    "no preference" and yields the full narrative slice, so a query is never
    accidentally narrowed to nothing.
    """
    if not roles:
        return NARRATIVE_SEARCH_TYPES
    selected: list[str] = []
    unknown: list[str] = []
    for raw_role in roles:
        role = (raw_role or "").strip().lower()
        if not role:
            continue
        mapped = NARRATIVE_ROLE_TYPES.get(role)
        if mapped is None:
            unknown.append(role)
            continue
        selected.extend(t for t in mapped if t not in selected)
    if unknown:
        raise ValueError(
            "unknown role(s): "
            + ", ".join(sorted(set(unknown)))
            + ". Valid roles: "
            + ", ".join(sorted(NARRATIVE_ROLE_TYPES))
        )
    return tuple(selected) or NARRATIVE_SEARCH_TYPES
