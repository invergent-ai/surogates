"""Shared types and helpers for KB source runners.

Every runner produces ``kb_raw_doc`` rows + Garage objects; the
add-vs-update-vs-unchanged decision logic and the actual writes are
identical regardless of where the bytes came from. The
:func:`upsert_raw_doc` helper here owns that pattern so the runners
focus on the source-specific work (file walking, URL fetching,
markitdown conversion) instead of duplicating I/O.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.storage.kb_storage import KbStorage

logger = logging.getLogger(__name__)


@dataclass
class SourceContext:
    """Per-source state passed from :mod:`surogates.jobs.kb_ingest` to a
    runner. Resolved by joining ``kb_source`` to its parent ``kb`` row
    so the runner has everything it needs in one struct.
    """

    id: UUID                        # kb_source.id
    kb_id: UUID                     # kb_source.kb_id
    kb_org_id: UUID | None          # kb.org_id (None for platform KBs)
    kb_name: str                    # kb.name
    kind: str                       # kb_source.kind
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestResult:
    """Per-run summary returned from a runner.

    Counts: how many docs were newly inserted, updated (content
    changed), unchanged (content_sha matched, no work done), and
    skipped (e.g. unreadable file, unsupported format).
    """

    docs_added: int = 0
    docs_updated: int = 0
    docs_unchanged: int = 0
    docs_skipped: int = 0
    bytes_written: int = 0

    @property
    def total(self) -> int:
        return (
            self.docs_added
            + self.docs_updated
            + self.docs_unchanged
            + self.docs_skipped
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "docs_added": self.docs_added,
            "docs_updated": self.docs_updated,
            "docs_unchanged": self.docs_unchanged,
            "docs_skipped": self.docs_skipped,
            "bytes_written": self.bytes_written,
            "total": self.total,
        }


class UpsertOutcome(Enum):
    """What :func:`upsert_raw_doc` did with a single doc."""

    ADDED = "added"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


async def upsert_raw_doc(
    ctx: SourceContext,
    *,
    bucket_path: str,
    data: bytes,
    title: Optional[str],
    url: Optional[str],
    session_factory: async_sessionmaker,
    storage: KbStorage,
    existing: dict[str, tuple[uuid.UUID, str]],
    result: IngestResult,
) -> UpsertOutcome:
    """Idempotently materialise one raw doc to Garage + Postgres.

    *existing* is a per-run snapshot of ``{path: (id, content_sha)}`` for
    the parent KB so we can decide add/update/unchanged in one pass
    without round-tripping per file.

    Updates *result* counters in-place. Returns the outcome for callers
    that want per-doc telemetry.
    """
    sha = hashlib.sha256(data).hexdigest()
    prior = existing.get(bucket_path)

    if prior is None:
        # New row + new bytes.
        await storage.write_entry(
            ctx.kb_org_id, ctx.kb_name, bucket_path, data,
        )
        async with session_factory() as db:
            await db.execute(
                text(
                    "INSERT INTO kb_raw_doc "
                    "(id, kb_id, source_id, path, content_sha, title, url) "
                    "VALUES (:id, :kb_id, :source_id, :path, :sha, "
                    "        :title, :url)"
                ),
                {
                    "id": uuid.uuid4(),
                    "kb_id": ctx.kb_id,
                    "source_id": ctx.id,
                    "path": bucket_path,
                    "sha": sha,
                    "title": title,
                    "url": url,
                },
            )
            await db.commit()
        result.docs_added += 1
        result.bytes_written += len(data)
        return UpsertOutcome.ADDED

    if prior[1] != sha:
        # Content changed → re-upload + update row.
        await storage.write_entry(
            ctx.kb_org_id, ctx.kb_name, bucket_path, data,
        )
        async with session_factory() as db:
            await db.execute(
                text(
                    "UPDATE kb_raw_doc "
                    "SET content_sha = :sha, "
                    "    title = :title, "
                    "    url = :url, "
                    "    ingested_at = NOW() "
                    "WHERE id = :id"
                ),
                {
                    "sha": sha,
                    "title": title,
                    "url": url,
                    "id": prior[0],
                },
            )
            await db.commit()
        result.docs_updated += 1
        result.bytes_written += len(data)
        return UpsertOutcome.UPDATED

    # Same hash → skip both upload and DB update.
    result.docs_unchanged += 1
    return UpsertOutcome.UNCHANGED


async def load_existing_raw_docs(
    kb_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker,
) -> dict[str, tuple[uuid.UUID, str]]:
    """Snapshot ``{path: (id, content_sha)}`` for the KB's raw docs.

    Called once at the start of a run; passed to
    :func:`upsert_raw_doc` for each file the runner processes so the
    add/update/unchanged decision is a dict lookup not a DB query.
    """
    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id, path, content_sha "
                    "FROM kb_raw_doc WHERE kb_id = :kb_id"
                ),
                {"kb_id": kb_id},
            )
        ).all()
    return {r.path: (r.id, r.content_sha) for r in rows}
