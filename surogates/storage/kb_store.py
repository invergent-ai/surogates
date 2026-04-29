"""Knowledge-base retrieval engine.

Used by the ``kb_search`` and ``kb_read`` HARNESS tools.

Tenant scoping is applied two ways on every query:
  - **Belt**: an explicit ``WHERE (kb.org_id = :org_id OR kb.org_id IS NULL)``
    filter in application code (this module).
  - **Suspenders**: row-level security policies on every KB table (see
    ``surogates/db/kb.sql``) that filter on the ``app.org_id`` session GUC
    when set, and only show platform rows otherwise.

Step 3 limitation: byte content is not yet fetched from object storage —
``read_entry`` returns the row's metadata plus a stub note. The
storage-backend wiring lands in step 4 alongside the ``markdown_dir``
ingest path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)


@dataclass
class KbHit:
    """A single hit returned from :meth:`KbStore.search`."""

    kb_name: str
    document_path: str
    snippet: str
    score: float


class KbStore:
    """Retrieval engine over the KB schema.

    Currently runs a BM25-only branch via Postgres ``tsvector @@
    plainto_tsquery``. Vector search via pgvector lands in step 5 when
    an embedding service is threaded through the harness; the merge
    (reciprocal-rank fusion) is added at the same time. Until then,
    ``score`` is the BM25 ``ts_rank_cd``.
    """

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        org_id: UUID,
        query: str,
        kb_name: Optional[str] = None,
        top_k: int = 5,
    ) -> list[KbHit]:
        """Search KBs visible to *org_id* (own + platform).

        Args:
            org_id: tenant org id (drawn from the harness ``tenant``
                kwarg, never from a tool argument).
            query: natural-language query string.
            kb_name: optional KB name to scope the search. Omit for
                federated search across every KB the org can read.
            top_k: maximum hits per KB. Federated results are merged
                across KBs, not capped at ``top_k`` total — so one big
                KB cannot drown smaller ones.

        Returns: list of :class:`KbHit`, ordered by score descending,
        capped at ``top_k`` (single-KB) or ``4 * top_k`` (federated).
        Returns ``[]`` for whitespace queries, unknown KB names, or
        empty corpora.
        """
        if not query or not query.strip():
            return []
        if top_k < 1:
            top_k = 1

        # 1. Resolve the candidate KBs.
        async with self._session_factory() as db:
            kb_sql = (
                "SELECT id, name FROM kb "
                "WHERE (org_id = :org_id OR org_id IS NULL) "
            )
            params: dict[str, object] = {"org_id": org_id}
            if kb_name is not None:
                kb_sql += "AND name = :name"
                params["name"] = kb_name
            kb_rows = (await db.execute(text(kb_sql), params)).all()

        if not kb_rows:
            return []
        kb_ids = [r.id for r in kb_rows]

        # 2. BM25 query against the matching chunks. Over-fetch so the
        # per-KB bucketing below has enough candidates per KB.
        async with self._session_factory() as db:
            sql = text(
                """
                SELECT
                    we.path        AS path,
                    we.kb_id       AS kb_id,
                    kb.name        AS kb_name,
                    c.content      AS content,
                    ts_rank_cd(c.tsv, plainto_tsquery('english', :q)) AS score
                FROM kb_chunk c
                JOIN kb_wiki_entry we ON we.id = c.wiki_entry_id
                JOIN kb ON kb.id = we.kb_id
                WHERE we.kb_id = ANY(:kb_ids)
                  AND c.tsv @@ plainto_tsquery('english', :q)
                ORDER BY score DESC
                LIMIT :overfetch
                """
            )
            rows = (
                await db.execute(
                    sql,
                    {
                        "q": query,
                        "kb_ids": kb_ids,
                        "overfetch": top_k * len(kb_ids) + top_k,
                    },
                )
            ).all()

        # 3. Per-KB top_k slicing prevents a high-volume KB from
        # crowding others off the result list.
        per_kb: dict[UUID, list[KbHit]] = {}
        for r in rows:
            bucket = per_kb.setdefault(r.kb_id, [])
            if len(bucket) >= top_k:
                continue
            bucket.append(
                KbHit(
                    kb_name=r.kb_name,
                    document_path=r.path,
                    snippet=_make_snippet(r.content, query),
                    score=float(r.score),
                )
            )

        merged = [h for hits in per_kb.values() for h in hits]
        merged.sort(key=lambda h: h.score, reverse=True)
        if kb_name is not None:
            return merged[:top_k]
        # Federated cap: at most 4 KBs * top_k, so a malformed query
        # can never blow up the assistant's context window.
        return merged[: min(len(per_kb), 4) * top_k]

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def read_entry(
        self,
        org_id: UUID,
        path: str,
        kb_name: Optional[str] = None,
    ) -> Optional[dict]:
        """Resolve a wiki or raw entry within the calling org's KBs.

        Path forms:
          - With ``kb_name`` set: ``path`` is relative to the KB root
            (e.g. ``wiki/summaries/foo.md``).
          - Without ``kb_name``: ``path`` must include the KB name as
            its first segment (e.g.
            ``invergent-docs/wiki/summaries/foo.md``).

        Returns a dict ``{kb_name, path, kind}`` where ``kind`` is
        ``'wiki'`` or ``'raw'`` if the row exists and the org can see
        the parent KB. Returns ``None`` otherwise (kb name unknown,
        entry not registered, or cross-tenant access).

        Step 3 limitation: byte content is not fetched from object
        storage. The handler returns the registration metadata so the
        agent can confirm the entry exists; full content reads land in
        step 4 alongside ingestion + storage-backend threading.
        """
        if not path:
            return None
        if kb_name is None:
            if "/" not in path:
                return None
            kb_name_resolved, rest = path.split("/", 1)
        else:
            kb_name_resolved = kb_name
            rest = path

        async with self._session_factory() as db:
            kb_row = (
                await db.execute(
                    text(
                        "SELECT id, org_id FROM kb "
                        "WHERE name = :name "
                        "  AND (org_id = :org_id OR org_id IS NULL)"
                    ),
                    {"name": kb_name_resolved, "org_id": org_id},
                )
            ).first()
            if kb_row is None:
                return None

            entry = (
                await db.execute(
                    text(
                        "SELECT 'wiki' AS kind, path FROM kb_wiki_entry "
                        "WHERE kb_id = :kb_id AND path = :path "
                        "UNION ALL "
                        "SELECT 'raw' AS kind, path FROM kb_raw_doc "
                        "WHERE kb_id = :kb_id AND path = :path "
                        "LIMIT 1"
                    ),
                    {"kb_id": kb_row.id, "path": rest},
                )
            ).first()
            if entry is None:
                return None

        return {
            "kb_name": kb_name_resolved,
            "path": rest,
            "kind": entry.kind,
        }


def _make_snippet(content: str, query: str, ctx_chars: int = 160) -> str:
    """Build a ~ctx_chars window centred on the first matching query
    term. Falls back to the leading slice when no term matches.
    """
    if len(content) <= 2 * ctx_chars:
        return content
    cl = content.lower()
    pos = -1
    for term in query.lower().split():
        p = cl.find(term)
        if p >= 0 and (pos == -1 or p < pos):
            pos = p
    if pos < 0:
        return content[: 2 * ctx_chars] + "..."
    start = max(0, pos - ctx_chars)
    end = min(len(content), pos + ctx_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(content) else ""
    return f"{prefix}{content[start:end]}{suffix}"
