# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
#
"""Pin the narrative FTS index DDL against the query-side helpers.

Postgres uses an expression index only when the query's expression parses
to the same tree, and a partial index only when the query's predicate
implies the index's ``WHERE``. Neither mismatch raises — the planner just
falls back to a sequential scan over the whole ``events`` table. These
tests are the only thing standing between a one-character edit and a
silent performance cliff.
"""
from __future__ import annotations

import re
from pathlib import Path

from surogates.db.narrative import (
    NARRATIVE_FTS_CONFIG,
    NARRATIVE_ROLE_TYPES,
    NARRATIVE_SEARCH_TYPES,
    narrative_text_sql,
    narrative_tsquery_sql,
    narrative_tsvector_sql,
    narrative_type_list_sql,
)

_SQL_PATH = Path(__file__).resolve().parents[2] / "surogates" / "db" / "observability.sql"
_INDEX_NAME = "idx_events_narrative_fts"


def _index_statement() -> str:
    sql = _SQL_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"CREATE INDEX IF NOT EXISTS {_INDEX_NAME}\b(.*?);",
        sql,
        re.DOTALL,
    )
    assert match, f"{_INDEX_NAME} is missing from {_SQL_PATH.name}"
    return match.group(1)


def _normalize(sql: str) -> str:
    """Collapse whitespace so formatting differences don't fail the test."""
    return re.sub(r"\s+", " ", sql).strip()


def test_index_expression_matches_the_query_helper() -> None:
    assert _normalize(narrative_tsvector_sql()) in _normalize(_index_statement())


def test_index_predicate_matches_the_searchable_types() -> None:
    statement = _normalize(_index_statement())
    expected = _normalize(f"WHERE type IN ({narrative_type_list_sql()})")
    assert expected in statement


def test_index_is_gin() -> None:
    assert "USING gin" in _normalize(_index_statement())


def test_llm_delta_is_never_indexed() -> None:
    """Deltas are one row per token chunk — indexing them is the cliff."""
    assert "llm.delta" not in NARRATIVE_SEARCH_TYPES
    assert "llm.delta" not in _index_statement()


def test_assistant_text_is_reachable() -> None:
    """``llm.response`` nests content under ``message``.

    A flat ``data->>'content'`` read returns NULL for every assistant turn,
    which is the defect that made ``role_filter="assistant"`` unmatchable.
    """
    assert "data->'message'->>'content'" in narrative_text_sql()


def test_recaps_are_searchable() -> None:
    text = narrative_text_sql()
    assert "data->>'recap'" in text
    assert "data->>'summary'" in text


def test_dead_result_key_is_not_searched() -> None:
    """``tool.result`` payloads carry ``content``; ``result`` never existed."""
    assert "'result'" not in narrative_text_sql()


def test_query_uses_websearch_to_tsquery() -> None:
    """plainto_tsquery discards OR/phrase/negation the tools document."""
    rendered = narrative_tsquery_sql("query")
    assert rendered.startswith("websearch_to_tsquery(")
    assert ":query" in rendered


def test_config_is_shared_between_index_and_query() -> None:
    assert f"'{NARRATIVE_FTS_CONFIG}'" in narrative_tsvector_sql()
    assert f"'{NARRATIVE_FTS_CONFIG}'" in narrative_tsquery_sql()


def test_prefix_qualifies_every_column_reference() -> None:
    prefixed = narrative_text_sql("e.")
    assert "e.data" in prefixed
    assert re.search(r"(?<![\w.])data->", prefixed) is None


def test_role_types_are_a_subset_of_searchable_types() -> None:
    for role, types in NARRATIVE_ROLE_TYPES.items():
        for event_type in types:
            assert event_type in NARRATIVE_SEARCH_TYPES, (
                f"role {role!r} maps to {event_type!r}, which the index "
                "does not cover"
            )
