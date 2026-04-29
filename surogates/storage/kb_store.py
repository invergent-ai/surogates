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
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.storage.backend import StorageBackend
from surogates.storage.embeddings import EmbeddingClient, vector_literal
from surogates.storage.kb_storage import KbStorage

logger = logging.getLogger(__name__)

#: Reciprocal-rank-fusion smoothing constant. 60 is the value from the
#: Cormack-Clarke-Buettcher paper that introduced RRF; widely re-used
#: (Vespa, Elastic, Weaviate, OpenSearch).
RRF_K = 60


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

    def __init__(
        self,
        session_factory: async_sessionmaker,
        storage_backend: StorageBackend | None = None,
        embedder: EmbeddingClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._kb_storage = (
            KbStorage(storage_backend) if storage_backend is not None else None
        )
        self._embedder = embedder

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        org_id: UUID,
        query: str,
        kb_name: Optional[str] = None,
        top_k: int = 5,
        agent_id: Optional[str] = None,
    ) -> list[KbHit]:
        """Search KBs visible to *org_id* (own + platform).

        Hybrid retrieval:
          - **Lexical (BM25)** via ``ts_rank_cd(tsv, plainto_tsquery(q))``.
          - **Vector (cosine)** via pgvector ``embedding <=> :qvec`` —
            only when an :class:`EmbeddingClient` was passed to the
            store and the query embeds successfully. Without an
            embedder, retrieval is BM25-only.
          - Merged via reciprocal-rank-fusion with constant
            ``RRF_K=60`` so a chunk that ranks high in either branch
            wins, and a chunk that ranks high in both wins decisively.

        Per-KB top_k bucketing prevents a high-volume KB from
        drowning federated results.

        Per-agent grants:
          - When ``agent_id`` is provided (the harness always supplies
            it), only KBs that are platform-shared OR have an
            ``agent_kb_grant`` row for this agent are searched. Other
            org KBs are invisible to the agent even though they're
            visible to the tenant.
          - When ``agent_id`` is ``None`` (direct admin / test
            dispatch), the legacy "all-org-visible" semantics apply.
        """
        if not query or not query.strip():
            return []
        if top_k < 1:
            top_k = 1

        # 1. Resolve the candidate KBs.
        kb_rows = await self._resolve_visible_kbs(
            org_id=org_id,
            kb_name=kb_name,
            agent_id=agent_id,
        )

        if not kb_rows:
            return []
        kb_ids = [r.id for r in kb_rows]

        # 2a. BM25 branch.
        overfetch = top_k * len(kb_ids) + top_k
        bm25_hits = await self._bm25_search(query, kb_ids, overfetch)

        # 2b. Vector branch (only if embedder is wired). Embedding
        # failures (network, dim mismatch, etc.) degrade gracefully to
        # BM25-only — we don't want a transient embedding outage to
        # take retrieval offline.
        vec_hits: list[_RankedChunk] = []
        if self._embedder is not None:
            try:
                [qvec] = await self._embedder.embed([query])
                vec_hits = await self._vector_search(qvec, kb_ids, overfetch)
            except Exception:  # noqa: BLE001 - intentionally broad
                logger.exception(
                    "kb_search: vector branch failed; degrading to BM25-only"
                )

        if not bm25_hits and not vec_hits:
            return []

        # 3. RRF merge by chunk_id.
        merged = _reciprocal_rank_fusion(bm25_hits, vec_hits)

        # 4. Per-KB top_k bucketing.
        per_kb: dict[UUID, list[KbHit]] = {}
        for chunk_id, rrf_score in merged:
            kb_id = _CHUNK_INDEX[chunk_id].kb_id
            bucket = per_kb.setdefault(kb_id, [])
            if len(bucket) >= top_k:
                continue
            row = _CHUNK_INDEX[chunk_id]
            bucket.append(
                KbHit(
                    kb_name=row.kb_name,
                    document_path=row.path,
                    snippet=_make_snippet(row.content, query),
                    score=rrf_score,
                )
            )

        # Reset the per-call chunk index (this is a module-private
        # cache scoped to one call; not worth thread-locals).
        _CHUNK_INDEX.clear()

        results = [h for hits in per_kb.values() for h in hits]
        results.sort(key=lambda h: h.score, reverse=True)
        if kb_name is not None:
            return results[:top_k]
        # Federated cap: at most 4 KBs × top_k, so a misuse of `kb=None`
        # can't blow up the assistant's context window.
        return results[: min(len(per_kb), 4) * top_k]

    # ------------------------------------------------------------------
    # Visibility resolver — the single place agent grants are enforced
    # ------------------------------------------------------------------

    async def _resolve_visible_kbs(
        self,
        *,
        org_id: UUID,
        kb_name: Optional[str],
        agent_id: Optional[str],
    ) -> list:
        """Return ``[(id, name, org_id)]`` for every KB visible to the
        caller, after applying tenant scope + per-agent grants.

        Visibility rules:
          * Platform KBs (``org_id IS NULL``) always visible.
          * Org KBs visible only when ``kb.org_id = :org_id``.
          * When *agent_id* is set (string — matches
            ``session.agent_id``), org KBs additionally require an
            ``agent_kb_grant`` row matching ``(kb_id, agent_id)``.
            ``None`` or empty string skips the grant check (legacy
            admin / direct-test path).
        """
        params: dict[str, object] = {"org_id": org_id}
        if agent_id is not None and agent_id != "":
            params["agent_id"] = agent_id
            grant_clause = (
                "(kb.org_id = :org_id AND EXISTS ("
                "  SELECT 1 FROM agent_kb_grant g "
                "  WHERE g.kb_id = kb.id AND g.agent_id = :agent_id"
                "))"
            )
        else:
            grant_clause = "kb.org_id = :org_id"

        sql = (
            "SELECT id, name, org_id FROM kb "
            f"WHERE (kb.org_id IS NULL OR {grant_clause}) "
        )
        if kb_name is not None:
            sql += "AND kb.name = :name"
            params["name"] = kb_name

        async with self._session_factory() as db:
            return list((await db.execute(text(sql), params)).all())

    # ------------------------------------------------------------------
    # Search branches
    # ------------------------------------------------------------------

    async def _bm25_search(
        self,
        query: str,
        kb_ids: list[UUID],
        limit: int,
    ) -> list["_RankedChunk"]:
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT
                            c.id           AS chunk_id,
                            we.path        AS path,
                            we.kb_id       AS kb_id,
                            kb.name        AS kb_name,
                            c.content      AS content
                        FROM kb_chunk c
                        JOIN kb_wiki_entry we ON we.id = c.wiki_entry_id
                        JOIN kb ON kb.id = we.kb_id
                        WHERE we.kb_id = ANY(:kb_ids)
                          AND c.tsv @@ plainto_tsquery('english', :q)
                        ORDER BY ts_rank_cd(c.tsv, plainto_tsquery('english', :q)) DESC
                        LIMIT :limit
                        """
                    ),
                    {"q": query, "kb_ids": kb_ids, "limit": limit},
                )
            ).all()
        ranked: list[_RankedChunk] = []
        for rank, r in enumerate(rows):
            row = _RankedChunk(
                chunk_id=r.chunk_id,
                kb_id=r.kb_id,
                kb_name=r.kb_name,
                path=r.path,
                content=r.content,
                rank=rank,
            )
            ranked.append(row)
            _CHUNK_INDEX[r.chunk_id] = row
        return ranked

    async def _vector_search(
        self,
        qvec: list[float],
        kb_ids: list[UUID],
        limit: int,
    ) -> list["_RankedChunk"]:
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT
                            c.id           AS chunk_id,
                            we.path        AS path,
                            we.kb_id       AS kb_id,
                            kb.name        AS kb_name,
                            c.content      AS content
                        FROM kb_chunk c
                        JOIN kb_wiki_entry we ON we.id = c.wiki_entry_id
                        JOIN kb ON kb.id = we.kb_id
                        WHERE we.kb_id = ANY(:kb_ids)
                          AND c.embedding IS NOT NULL
                        ORDER BY c.embedding <=> (:qvec)::vector
                        LIMIT :limit
                        """
                    ),
                    {
                        "qvec": vector_literal(qvec),
                        "kb_ids": kb_ids,
                        "limit": limit,
                    },
                )
            ).all()
        ranked: list[_RankedChunk] = []
        for rank, r in enumerate(rows):
            row = _RankedChunk(
                chunk_id=r.chunk_id,
                kb_id=r.kb_id,
                kb_name=r.kb_name,
                path=r.path,
                content=r.content,
                rank=rank,
            )
            ranked.append(row)
            # First branch wins for the per-call index — vector branch
            # adds chunks the BM25 branch may not have surfaced.
            _CHUNK_INDEX.setdefault(r.chunk_id, row)
        return ranked

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def read_entry(
        self,
        org_id: UUID,
        path: str,
        kb_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Resolve a wiki or raw entry within the calling org's KBs and
        fetch its bytes from object storage.

        Path forms:
          - With ``kb_name`` set: ``path`` is relative to the KB root
            (e.g. ``wiki/summaries/foo.md``).
          - Without ``kb_name``: ``path`` must include the KB name as
            its first segment (e.g.
            ``invergent-docs/wiki/summaries/foo.md``).

        Returns ``{kb_name, path, kind, content}``. ``kind`` is
        ``'wiki'`` or ``'raw'`` based on which table holds the row.
        ``content`` is the decoded text from object storage when the
        backend is wired and the object exists; an explanatory string
        otherwise (no backend wired, or DB row exists but the bucket
        object is missing — usually a partial-ingest failure).

        Returns ``None`` if the row is not registered or the org cannot
        see the parent KB (kb name unknown, cross-tenant access, etc.).
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

        # Resolve through the same grants-aware filter as search().
        # This means an agent that lost a grant after kb_search returned
        # a path can still NOT then read the entry, closing the
        # time-of-check / time-of-use window.
        kb_candidates = await self._resolve_visible_kbs(
            org_id=org_id,
            kb_name=kb_name_resolved,
            agent_id=agent_id,
        )
        if not kb_candidates:
            return None
        kb_row = kb_candidates[0]
        async with self._session_factory() as db:

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

        # Best-effort byte fetch. Note: ``kb_row.org_id`` (NOT the
        # caller's ``org_id``) is the right value for bucket routing —
        # platform KBs (org_id IS NULL) live in the platform bucket.
        content: str
        if self._kb_storage is None:
            content = (
                "[storage backend not wired in this context; entry "
                "registration confirmed but bytes unavailable]"
            )
        else:
            data = await self._kb_storage.read_entry(
                kb_row.org_id, kb_name_resolved, rest,
            )
            if data is None:
                content = (
                    "[entry registered in DB but no object found at "
                    f"bucket key for path={rest!r} — likely a partial "
                    "ingest; please re-run the source sync]"
                )
            else:
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    content = (
                        "[binary content; "
                        f"{len(data)} bytes]"
                    )

        return {
            "kb_name": kb_name_resolved,
            "path": rest,
            "kind": entry.kind,
            "content": content,
        }


@dataclass
class _RankedChunk:
    """Per-branch chunk row + its rank within that branch's result list."""

    chunk_id: UUID
    kb_id: UUID
    kb_name: str
    path: str
    content: str
    rank: int  # 0-indexed; 0 is best


# Per-call chunk lookup table populated by the search branches and
# read during RRF merge. Cleared at the end of every search() call.
# Module-private, single-thread (one search at a time per task).
_CHUNK_INDEX: dict[UUID, _RankedChunk] = {}


def _reciprocal_rank_fusion(
    *branch_lists: list[_RankedChunk],
) -> list[tuple[UUID, float]]:
    """Merge multiple ranked lists via reciprocal-rank-fusion.

    For each chunk that appears in at least one branch, its RRF score
    is ``sum(1 / (RRF_K + rank))`` across all branches it appears in.
    Returns ``[(chunk_id, score), ...]`` sorted by score descending.

    A chunk that ranks highly in either branch alone gets a non-zero
    score; a chunk that ranks highly in BOTH gets ~2× the score and
    rises to the top, which is the property that makes RRF work better
    than either branch in isolation.
    """
    scores: dict[UUID, float] = {}
    for branch in branch_lists:
        for hit in branch:
            scores[hit.chunk_id] = (
                scores.get(hit.chunk_id, 0.0)
                + 1.0 / (RRF_K + hit.rank)
            )
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


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
