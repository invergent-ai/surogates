"""Remove the ``llm.delta`` rows of finished model replies from the event log.

A worker whose deltas go to the log (``delta_transport: log``) writes each
streamed piece of a reply as an ``llm.delta`` row (before coalescing, one per
chunk: about nine in ten rows of ``events``).  Nothing reads them once their
model call is over: replay, context rebuild, search and every page of the
log leave them out, and the call's ``llm.response`` / ``llm.thinking`` hold
the text.  This removes them, for an operator who decides to; nothing runs it
on its own (``surogates purge-llm-deltas``, a dry run unless ``--execute``).

One thing still read them: replay counts the reasoning chunks of an
``llm.thinking`` that carries neither ``reasoning_delta_count`` nor
``reasoning_tokens`` (rows written before the harness stored the count) from
the ``llm.delta`` rows before it (``SessionStore._restore_reasoning_counts``).
So before a session's rows go, each such ``llm.thinking`` gets the count
written into it, as the harness writes it today; replay then shows the same
number without the rows.

It works in batches by id, each its own transaction, so it holds no long
lock and can be stopped and run again.  Only rows older than *older_than*
(an hour at least) whose call is over (an event that ends a model call
follows them in their session) are touched: no reply still streaming is cut.  Disk space comes back to
Postgres for reuse once autovacuum has run; to give it back to the
operating system the table needs ``VACUUM FULL`` or ``pg_repack``, which is
the operator's call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

#: Writes ``reasoning_delta_count`` into the ``llm.thinking`` rows with ids in
#: ``(:lo, :hi]`` that carry no count, from their reasoning ``llm.delta`` rows,
#: each standing for its ``chunks`` (the count
#: ``SessionStore._restore_reasoning_counts`` makes, for many sessions at once).  A row with no such ``llm.delta`` rows is left as it is.
_BACKFILL_SQL = text("""
WITH thinking AS (
    SELECT t.id, t.session_id,
           (SELECT max(r.id) FROM events r
             WHERE r.session_id = t.session_id AND r.type = 'llm.request' AND r.id < t.id) AS request_id
      FROM events t
     WHERE t.id > :lo AND t.id <= :hi
       AND t.type = 'llm.thinking'
       AND t.data->'reasoning_delta_count' IS NULL
       AND t.data->'reasoning_tokens' IS NULL
), counted AS (
    SELECT thinking.id,
           (SELECT COALESCE(sum(COALESCE((d.data->>'chunks')::int, 1)), 0) FROM events d
             WHERE d.session_id = thinking.session_id AND d.type = 'llm.delta'
               AND d.id > COALESCE(thinking.request_id, 0) AND d.id < thinking.id
               AND COALESCE(d.data->>'reasoning', '') <> '') AS n
      FROM thinking
)
UPDATE events e
   SET data = e.data || jsonb_build_object('reasoning_delta_count', counted.n)
  FROM counted
 WHERE e.id = counted.id AND counted.n > 0
""")

#: A delta row whose model call is over: an event that ends a call (or a
#: later one) follows it in its session.
_FINISHED = """
       AND EXISTS (SELECT 1 FROM events b
                    WHERE b.session_id = d.session_id AND b.id > d.id
                      AND b.type IN ('llm.request', 'llm.thinking', 'llm.response', 'harness.crash',
                                     'session.complete', 'session.fail', 'session.pause', 'session.stopped'))
"""

#: One batch of finished ``llm.delta`` rows older than ``:cutoff``, by id after ``:lo``, up to ``:hi``.
_DELETE_SQL = text(f"""
WITH batch AS (
    SELECT d.id FROM events d
     WHERE d.id > :lo AND d.id <= :hi AND d.type = 'llm.delta' AND d.created_at < :cutoff
     {_FINISHED}
     ORDER BY d.id
     LIMIT :batch
)
DELETE FROM events e USING batch WHERE e.id = batch.id
RETURNING e.id
""")

_COUNT_SQL = text(f"""
SELECT count(*) FROM events d
 WHERE d.type = 'llm.delta' AND d.created_at < :cutoff
 {_FINISHED}
""")

#: The youngest rows a run may touch: a reply streams for minutes at most.
MIN_AGE = timedelta(hours=1)

#: How many ids one backfill transaction covers.
_BACKFILL_WINDOW = 50_000


@dataclass
class PurgeReport:
    """What a run found and did."""

    candidates: int = 0
    deleted: int = 0
    thinking_counts_written: int = 0
    dry_run: bool = True


async def purge_llm_delta_rows(
    engine: AsyncEngine,
    *,
    older_than: timedelta = timedelta(days=1),
    batch: int = 5000,
    execute: bool = False,
    progress: Callable[[PurgeReport], None] | None = None,
) -> PurgeReport:
    """Remove the ``llm.delta`` rows of finished calls older than *older_than* (one hour at
    least); without *execute*, only count them.

    First every ``llm.thinking`` row that replay would count reasoning chunks
    for gets its count written (all of them, whatever their age, so a call
    that straddles the cutoff keeps its whole count); then the rows go, a
    batch per transaction.  *progress*, when given, is called with the
    report after every transaction.
    """
    if older_than < MIN_AGE:
        raise ValueError(f"older_than must be at least {MIN_AGE}: a younger row may belong to a reply still streaming")
    report = PurgeReport(dry_run=not execute)
    seconds = int(older_than.total_seconds())
    async with engine.connect() as conn:
        cutoff = (await conn.execute(
            text("SELECT (now() - make_interval(secs => :secs))::timestamp"), {"secs": seconds},
        )).scalar_one()
        report.candidates = (await conn.execute(_COUNT_SQL, {"cutoff": cutoff})).scalar_one()
        # Rows written after this run starts are past its range.
        top = (await conn.execute(text("SELECT COALESCE(max(id), 0) FROM events"))).scalar_one()
    if not execute or report.candidates == 0:
        return report

    lo = 0
    while lo < top:
        hi = min(lo + _BACKFILL_WINDOW, top)
        async with engine.begin() as conn:
            report.thinking_counts_written += (await conn.execute(
                _BACKFILL_SQL, {"lo": lo, "hi": hi},
            )).rowcount or 0
        lo = hi
        if progress is not None:
            progress(report)

    lo = 0
    while True:
        async with engine.begin() as conn:
            ids = [row[0] for row in (await conn.execute(
                _DELETE_SQL, {"lo": lo, "hi": top, "cutoff": cutoff, "batch": batch},
            )).all()]
        if not ids:
            break
        report.deleted += len(ids)
        lo = max(ids)
        if progress is not None:
            progress(report)
    return report
