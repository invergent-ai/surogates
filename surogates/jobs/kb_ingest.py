"""KB ingestion dispatcher.

Resolves a ``kb_source.id`` to its runner module, takes a per-source
Postgres advisory lock so concurrent ingests of the same source
serialize, runs the ingest, and updates the row's ``last_status`` /
``last_synced_at`` / ``last_error`` accordingly.

Called from:

  - The HTTP route ``POST /v1/kb/{id}/sources/{sid}/sync`` (synchronous).
  - The background scheduler (later step).
  - Tests + CLI.
"""

from __future__ import annotations

import importlib
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.jobs.kb_sources._base import IngestResult, SourceContext
from surogates.storage.backend import StorageBackend

logger = logging.getLogger(__name__)


#: Map ``kb_source.kind`` → fully-qualified runner module name.
#: Each module must expose ``async def run(ctx, *, session_factory,
#: storage_backend) -> IngestResult``.
RUNNERS: dict[str, str] = {
    "markdown_dir": "surogates.jobs.kb_sources.markdown_dir",
    "web_scraper": "surogates.jobs.kb_sources.web_scraper",
    "file_upload": "surogates.jobs.kb_sources.file_upload",
}


# ---------------------------------------------------------------------------
# Advisory lock keying
# ---------------------------------------------------------------------------


def advisory_key_for(source_id: UUID) -> int:
    """Deterministic ``bigint`` advisory-lock key from a UUID.

    PostgreSQL advisory locks take a 64-bit signed integer key (range
    ``[-2**63, 2**63-1]``). asyncpg rejects unsigned values that
    exceed ``2**63-1`` even though Postgres would happily accept them
    as the high-bit-set negative form, so we mask down to 63 bits.
    Different sources still get different keys (uniform entropy from
    the UUID's high half) and the same source maps to the same key
    across runs.
    """
    return (source_id.int >> 64) & 0x7FFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class IngestLocked(RuntimeError):
    """Raised when ``block=False`` and another worker holds the lock."""


async def run_ingest(
    source_id: UUID,
    *,
    session_factory: async_sessionmaker,
    storage_backend: StorageBackend,
    block: bool = False,
) -> IngestResult:
    """Run a single ingest pass for *source_id*.

    Holds a per-source advisory lock for the duration so two
    concurrent calls on the same source serialise. With ``block=True``
    this waits for the lock; with ``block=False`` (default) it raises
    :class:`IngestLocked` immediately if another worker has it.

    Updates ``kb_source`` status fields:
      - on entry: ``last_status='running'``, ``last_error=NULL``.
      - on success: ``last_status='success'``, ``last_synced_at=NOW()``.
      - on failure: ``last_status='failed'``, ``last_error=<exc>``,
        ``last_synced_at=NOW()`` (so we still record the attempt
        timestamp), then re-raises the original exception.
    """
    key = advisory_key_for(source_id)

    # Acquire on a dedicated session that we hold open for the full
    # ingest. ``pg_advisory_lock`` is session-scoped, so the lock is
    # released the moment this session goes back to the pool.
    async with session_factory() as lock_session:
        if block:
            await lock_session.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": key},
            )
            await lock_session.commit()
            acquired = True
        else:
            row = (
                await lock_session.execute(
                    text("SELECT pg_try_advisory_lock(:key) AS got"),
                    {"key": key},
                )
            ).first()
            await lock_session.commit()
            acquired = bool(row.got) if row is not None else False

        if not acquired:
            raise IngestLocked(
                f"another worker is currently ingesting source {source_id}"
            )

        try:
            return await _run_under_lock(
                source_id,
                session_factory=session_factory,
                storage_backend=storage_backend,
            )
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": key},
            )
            await lock_session.commit()


async def _run_under_lock(
    source_id: UUID,
    *,
    session_factory: async_sessionmaker,
    storage_backend: StorageBackend,
) -> IngestResult:
    """The lock-protected body. Loads the source, dispatches to the
    runner, and updates status + error in-place.
    """
    # 1. Load the source + parent KB context in one query.
    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT s.id, s.kb_id, s.kind, s.config, "
                    "       s.deleted_at, kb.name AS kb_name, "
                    "       kb.org_id AS kb_org_id "
                    "FROM kb_source s "
                    "JOIN kb ON kb.id = s.kb_id "
                    "WHERE s.id = :id"
                ),
                {"id": source_id},
            )
        ).first()

    if row is None:
        raise ValueError(f"kb_source not found: {source_id}")
    if row.deleted_at is not None:
        raise ValueError(f"kb_source is tombstoned: {source_id}")

    runner_path = RUNNERS.get(row.kind)
    if runner_path is None:
        # Mark failed and bail.
        await _set_status(
            session_factory,
            source_id,
            status="failed",
            error=f"unknown source kind: {row.kind!r}",
        )
        raise ValueError(f"unknown source kind: {row.kind!r}")

    ctx = SourceContext(
        id=row.id,
        kb_id=row.kb_id,
        kb_org_id=row.kb_org_id,
        kb_name=row.kb_name,
        kind=row.kind,
        config=dict(row.config) if row.config else {},
    )

    # 2. Mark running.
    await _set_status(
        session_factory, source_id, status="running", error=None,
    )

    # 3. Dispatch to the runner.
    try:
        runner = importlib.import_module(runner_path)
        result: IngestResult = await runner.run(
            ctx,
            session_factory=session_factory,
            storage_backend=storage_backend,
        )
    except Exception as exc:
        logger.exception("ingest failed for source %s", source_id)
        await _set_status(
            session_factory,
            source_id,
            status="failed",
            error=str(exc)[:1000],
            update_synced_at=True,
        )
        raise

    # 4. Mark success.
    await _set_status(
        session_factory,
        source_id,
        status="success",
        error=None,
        update_synced_at=True,
    )
    return result


async def _set_status(
    session_factory: async_sessionmaker,
    source_id: UUID,
    *,
    status: str,
    error: Optional[str],
    update_synced_at: bool = False,
) -> None:
    sql_parts = ["last_status = :status", "last_error = :error"]
    params: dict[str, object] = {
        "status": status,
        "error": error,
        "id": source_id,
    }
    if update_synced_at:
        sql_parts.append("last_synced_at = NOW()")
    set_clause = ", ".join(sql_parts)
    async with session_factory() as db:
        await db.execute(
            text(f"UPDATE kb_source SET {set_clause} WHERE id = :id"),
            params,
        )
        await db.commit()
