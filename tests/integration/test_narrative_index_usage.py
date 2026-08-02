# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
#
"""Prove the narrative FTS index is real and that queries can use it.

The parity unit tests pin the DDL text against the helpers, but text
matching cannot tell you whether Postgres agrees. These tests execute the
real statements against the real schema and read the planner's mind:

* the index exists after ``observability.sql`` has been applied,
* a query rendered from the shared helpers is *satisfiable* by it, and
* the searchable-text expression behaves as documented across the four
  payload shapes it has to cover.

A drifting expression does not raise — it just stops using the index — so
without an ``EXPLAIN`` assertion the cliff is invisible until production.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from surogates.db.narrative import (
    NARRATIVE_SEARCH_TYPES,
    narrative_tsquery_sql,
    narrative_tsvector_sql,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_INDEX_NAME = "idx_events_narrative_fts_v2"


async def test_index_exists(session_factory) -> None:
    async with session_factory() as db:
        found = (
            await db.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'events' AND indexname = :name"
                ),
                {"name": _INDEX_NAME},
            )
        ).scalar_one_or_none()

    assert found == _INDEX_NAME, (
        "observability.sql did not create the narrative FTS index"
    )


async def test_index_is_gin_and_partial(session_factory) -> None:
    async with session_factory() as db:
        definition = (
            await db.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": _INDEX_NAME},
            )
        ).scalar_one()

    assert "USING gin" in definition
    assert "WHERE" in definition
    for event_type in NARRATIVE_SEARCH_TYPES:
        assert event_type in definition
    assert "llm.delta" not in definition


async def test_planner_can_use_the_index_for_the_shared_query(
    session_factory,
) -> None:
    """The predicate the helpers render must be satisfiable by the index.

    Postgres will not choose a bitmap index scan on an empty table, so this
    asks the planner the narrower question it can always answer: does an
    index-only path exist for this exact expression? ``enable_seqscan=off``
    forces it to prove one or admit it cannot.
    """
    tsvector_sql = narrative_tsvector_sql("e.")
    tsquery_sql = narrative_tsquery_sql("q")
    type_list = ", ".join(f"'{t}'" for t in NARRATIVE_SEARCH_TYPES)

    async with session_factory() as db:
        await db.execute(text("SET LOCAL enable_seqscan = off"))
        plan_rows = (
            await db.execute(
                text(
                    f"""
                    EXPLAIN SELECT e.id
                    FROM events e
                    WHERE e.type IN ({type_list})
                      AND {tsvector_sql} @@ {tsquery_sql}
                    """
                ),
                {"q": "reagent"},
            )
        ).scalars().all()

    plan = "\n".join(plan_rows)
    assert _INDEX_NAME in plan, (
        "the planner cannot use the narrative index for the query the shared "
        f"helpers render — the expression or the type predicate has drifted:\n{plan}"
    )
    # The index name alone is not enough. This index is *partial*, so when
    # only the tsvector expression drifts the planner still uses it — for the
    # type predicate — and demotes the text match to a per-row ``Filter``.
    # That plan reads every narrative row and re-evaluates ``to_tsvector`` on
    # each, which is the sequential-scan cost this index exists to avoid, and
    # it contains the index name. Require the text match to be an index
    # condition, which is only true when the expressions agree.
    index_cond = "\n".join(
        line for line in plan_rows if "Index Cond" in line
    )
    assert "@@" in index_cond, (
        "the tsvector expression has drifted from the index: the text match "
        "was pushed to a per-row Filter instead of an Index Cond, so every "
        f"narrative row is re-tokenised at query time:\n{plan}"
    )


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"content": "limiting reagent question"}, "reagent"),
        (
            {"message": {"role": "assistant", "content": "equivalence point"}},
            "equivalence",
        ),
        ({"turn_id": "t1", "recap": "covered molar ratios"}, "molar"),
        ({"turn_id": "t1", "summary": "checked the titration curve"}, "titration"),
    ],
    ids=["user-content", "assistant-nested", "recap", "iteration-summary"],
)
async def test_every_payload_shape_is_searchable(
    session_factory, payload: dict, needle: str,
) -> None:
    """One row per shape the concatenated expression has to reach."""
    tsvector_sql = narrative_tsvector_sql("")
    tsquery_sql = narrative_tsquery_sql("q")

    async with session_factory() as db:
        matched = (
            await db.execute(
                text(
                    f"SELECT {tsvector_sql} @@ {tsquery_sql} "
                    "FROM (SELECT CAST(:payload AS jsonb) AS data) AS e"
                ),
                {"payload": json.dumps(payload), "q": needle},
            )
        ).scalar_one()

    assert matched is True, f"{needle!r} not found in {payload!r}"


async def test_dead_result_key_is_not_searched(session_factory) -> None:
    """``tool.result`` carries ``content``; ``result`` was never written."""
    tsvector_sql = narrative_tsvector_sql("")
    tsquery_sql = narrative_tsquery_sql("q")

    async with session_factory() as db:
        matched = (
            await db.execute(
                text(
                    f"SELECT {tsvector_sql} @@ {tsquery_sql} "
                    "FROM (SELECT CAST(:payload AS jsonb) AS data) AS e"
                ),
                {"payload": '{"result": "orphaned text"}', "q": "orphaned"},
            )
        ).scalar_one()

    assert matched is False
