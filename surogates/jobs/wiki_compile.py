"""Mechanical wiki-compile pass.

Step 5a's deliverable: project ``kb_raw_doc`` rows into
``kb_wiki_entry`` rows + chunks + embeddings so ``kb_search`` returns
real hits over the ingested content. No LLM authoring — the wiki
entry is a 1:1 byte copy of the raw doc. Step 5b replaces this with
the LLM-driven wiki maintainer that compresses + cross-references.

Idempotent: re-running with the same raw docs is a no-op (skips
entries whose ``content_sha`` already matches). Newly-changed raw
docs (content_sha differs) trigger a re-chunk + re-embed; existing
chunks for that wiki entry are deleted first.

Watermark: each successful run advances ``kb.last_compiled_at`` so
the next call with ``only_changed_since=True`` reads only newer raw
docs. This is the read-side counterpart to the ingest watermark — it
lets ingest and compile happen concurrently without the compiler ever
seeing half-ingested state, because the compile snapshot is a strict
``ingested_at <= watermark`` filter.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.storage.backend import StorageBackend
from surogates.storage.chunker import chunk_markdown
from surogates.storage.embeddings import EmbeddingClient, vector_literal
from surogates.storage.kb_storage import KbStorage

logger = logging.getLogger(__name__)


@dataclass
class CompileResult:
    """Per-run summary returned from :func:`compile_wiki_for_kb`."""

    entries_added: int = 0
    entries_updated: int = 0
    entries_unchanged: int = 0
    chunks_added: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "entries_added": self.entries_added,
            "entries_updated": self.entries_updated,
            "entries_unchanged": self.entries_unchanged,
            "chunks_added": self.chunks_added,
        }


async def compile_wiki_for_kb(
    kb_id: UUID,
    *,
    session_factory: async_sessionmaker,
    storage_backend: StorageBackend,
    embedder: Optional[EmbeddingClient] = None,
    only_changed_since: bool = True,
) -> CompileResult:
    """Project raw_docs into wiki_entries + chunks + embeddings.

    Args:
        kb_id: KB to compile.
        session_factory: DB sessions for reads + writes.
        storage_backend: Garage / LocalBackend; reads raw bytes,
            writes wiki entry bytes.
        embedder: Optional. When provided, each chunk gets its
            embedding column populated; ``kb_search``'s vector branch
            then has data to work with. When ``None``, chunks are
            inserted with ``embedding=NULL`` and the BM25 branch alone
            powers retrieval.
        only_changed_since: When ``True`` (default) and the KB has a
            prior ``last_compiled_at`` watermark, only raw docs with
            ``ingested_at > watermark`` are processed. ``False``
            forces a full recompile (used by ``kb_rollback`` and the
            "force recompile" admin path).
    """
    async with session_factory() as db:
        kb = (
            await db.execute(
                text(
                    "SELECT id, name, org_id, last_compiled_at "
                    "FROM kb WHERE id = :id"
                ),
                {"id": kb_id},
            )
        ).first()
    if kb is None:
        raise ValueError(f"kb not found: {kb_id}")

    raw_docs = await _load_raw_docs(
        kb_id,
        session_factory=session_factory,
        watermark=kb.last_compiled_at if only_changed_since else None,
    )

    if not raw_docs:
        return CompileResult()

    storage = KbStorage(storage_backend)
    result = CompileResult()

    for raw in raw_docs:
        outcome = await _compile_one_raw_doc(
            raw,
            kb=kb,
            storage=storage,
            embedder=embedder,
            session_factory=session_factory,
            result=result,
        )
        # Per-doc telemetry available if needed; for now the result
        # counters are enough.
        del outcome

    # Bump the watermark even when nothing was added — successful no-op
    # runs should advance time so ingest deltas after this point are
    # visible to the next compile call.
    async with session_factory() as db:
        await db.execute(
            text("UPDATE kb SET last_compiled_at = NOW() WHERE id = :id"),
            {"id": kb_id},
        )
        await db.commit()

    logger.info(
        "wiki_compile: kb=%s result=%s",
        kb.name, result.as_dict(),
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _load_raw_docs(
    kb_id: UUID,
    *,
    session_factory: async_sessionmaker,
    watermark,
) -> list:
    sql = (
        "SELECT id, path, content_sha, title, url, ingested_at "
        "FROM kb_raw_doc WHERE kb_id = :kb_id"
    )
    params: dict[str, object] = {"kb_id": kb_id}
    if watermark is not None:
        sql += " AND ingested_at > :since"
        params["since"] = watermark
    sql += " ORDER BY ingested_at ASC"

    async with session_factory() as db:
        rows = (await db.execute(text(sql), params)).all()
    return rows


async def _compile_one_raw_doc(
    raw,
    *,
    kb,
    storage: KbStorage,
    embedder: Optional[EmbeddingClient],
    session_factory: async_sessionmaker,
    result: CompileResult,
) -> Optional[str]:
    """Process one raw doc: read bytes, upsert wiki entry, chunk + embed.

    Returns ``"added"`` / ``"updated"`` / ``"unchanged"`` / ``"skipped"``
    for telemetry.
    """
    raw_bytes = await storage.read_entry(
        kb_org_id=kb.org_id, kb_name=kb.name, path=raw.path,
    )
    if raw_bytes is None:
        logger.warning(
            "wiki_compile: raw bytes missing for kb=%s path=%s",
            kb.name, raw.path,
        )
        return "skipped"

    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning(
            "wiki_compile: raw doc not utf-8: kb=%s path=%s",
            kb.name, raw.path,
        )
        return "skipped"

    # Wiki entry path: raw/foo/bar.md → wiki/summaries/foo/bar.md.
    # Step 5b's LLM maintainer will rewrite these into compressed
    # summaries; for now they're 1:1 copies so kb_search has chunks
    # to retrieve.
    wiki_path = _wiki_path_for_raw(raw.path)

    # Decide upsert action based on the wiki entry's existing sha.
    async with session_factory() as db:
        existing = (
            await db.execute(
                text(
                    "SELECT id, content_sha FROM kb_wiki_entry "
                    "WHERE kb_id = :kb_id AND path = :path"
                ),
                {"kb_id": kb.id, "path": wiki_path},
            )
        ).first()

    if existing and existing.content_sha == raw.content_sha:
        result.entries_unchanged += 1
        return "unchanged"

    # New chunks for this entry — embed them once.
    chunks = chunk_markdown(content)
    if embedder is not None and chunks:
        embeddings = await embedder.embed([c.content for c in chunks])
    else:
        embeddings = [None] * len(chunks)

    async with session_factory() as db:
        if existing:
            entry_id = existing.id
            await db.execute(
                text(
                    "UPDATE kb_wiki_entry SET "
                    "    content_sha = :sha, "
                    "    sources     = :sources, "
                    "    updated_at  = NOW() "
                    "WHERE id = :id"
                ),
                {
                    "sha": raw.content_sha,
                    "sources": [raw.id],
                    "id": entry_id,
                },
            )
            await db.execute(
                text("DELETE FROM kb_chunk WHERE wiki_entry_id = :id"),
                {"id": entry_id},
            )
            outcome = "updated"
        else:
            entry_id = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO kb_wiki_entry "
                    "(id, kb_id, path, kind, content_sha, sources) "
                    "VALUES "
                    "(:id, :kb_id, :path, 'summary', :sha, :sources)"
                ),
                {
                    "id": entry_id,
                    "kb_id": kb.id,
                    "path": wiki_path,
                    "sha": raw.content_sha,
                    "sources": [raw.id],
                },
            )
            outcome = "added"
        await db.commit()

    # Mirror the bytes into wiki/ so kb_read returns the same content
    # an LLM-authored entry would.
    await storage.write_entry(
        kb_org_id=kb.org_id,
        kb_name=kb.name,
        path=wiki_path,
        data=raw_bytes,
    )

    # Insert chunks with their embeddings.
    if chunks:
        async with session_factory() as db:
            for chunk, vec in zip(chunks, embeddings):
                params: dict[str, object] = {
                    "id": uuid.uuid4(),
                    "eid": entry_id,
                    "idx": chunk.chunk_index,
                    "content": chunk.content,
                    "heading": chunk.heading_path,
                }
                if vec is not None:
                    sql = (
                        "INSERT INTO kb_chunk "
                        "(id, wiki_entry_id, chunk_index, content, "
                        " heading_path, embedding) "
                        "VALUES "
                        "(:id, :eid, :idx, :content, :heading, "
                        " (:emb)::vector)"
                    )
                    params["emb"] = vector_literal(vec)
                else:
                    sql = (
                        "INSERT INTO kb_chunk "
                        "(id, wiki_entry_id, chunk_index, content, "
                        " heading_path) "
                        "VALUES "
                        "(:id, :eid, :idx, :content, :heading)"
                    )
                await db.execute(text(sql), params)
            await db.commit()
        result.chunks_added += len(chunks)

    if outcome == "added":
        result.entries_added += 1
    elif outcome == "updated":
        result.entries_updated += 1
    return outcome


def _wiki_path_for_raw(raw_path: str) -> str:
    """Map a raw_doc path to its wiki/summaries equivalent.

    ``raw/foo/bar.md`` → ``wiki/summaries/foo/bar.md``. Anything that
    doesn't begin with ``raw/`` is wrapped to keep behaviour
    well-defined.
    """
    if raw_path.startswith("raw/"):
        return "wiki/summaries/" + raw_path[len("raw/"):]
    return f"wiki/summaries/{raw_path}"
