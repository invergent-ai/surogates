"""``file_upload`` source runner — convert files staged in the KB's
``holding/{source_id}/`` prefix into markdown via ``markitdown`` and
ingest them as ``kb_raw_doc`` rows.

Workflow:
  1. Client (UI / API caller) POSTs files to the multipart upload
     endpoint, which writes each file at
     ``{kb-bucket}/holding/{source_id}/{filename}`` and returns the
     file list. (See :mod:`surogates.api.routes.knowledge_bases`.)
  2. Client triggers the sync endpoint for that source.
  3. This runner walks the holding prefix, converts each file via
     markitdown, writes the converted markdown to ``raw/{filename}.md``,
     and upserts the raw_doc row. The original holding bytes stay in
     place so a re-run can re-convert without a re-upload.

Config schema (``kb_source.config``):

  - ``max_bytes_per_file`` (int): default 10 MiB. Files larger than
    this are skipped.
  - ``preserve_holding`` (bool): default True. When False, files are
    deleted from holding after successful ingest.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Optional

from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.jobs.kb_sources._base import (
    IngestResult,
    SourceContext,
    load_existing_raw_docs,
    upsert_raw_doc,
)
from surogates.storage.backend import StorageBackend
from surogates.storage.kb_storage import KbStorage

logger = logging.getLogger(__name__)

KIND = "file_upload"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB

# Inside a KB bucket:
#   holding/{source_id}/{filename}      ← uploaded bytes
#   raw/{filename-without-ext}.md       ← converted markdown
_HOLDING_PREFIX = "holding"
_RAW_PREFIX = "raw"


def holding_prefix_for(source_id) -> str:
    """Return the bucket-relative prefix for a source's uploaded files."""
    return f"{_HOLDING_PREFIX}/{source_id}"


async def run(
    ctx: SourceContext,
    *,
    session_factory: async_sessionmaker,
    storage_backend: StorageBackend,
) -> IngestResult:
    config = ctx.config or {}
    max_bytes = int(config.get("max_bytes_per_file") or DEFAULT_MAX_BYTES)
    preserve_holding = bool(config.get("preserve_holding", True))

    storage = KbStorage(storage_backend)
    bucket = storage.bucket_for(ctx.kb_org_id)
    holding = storage.key_for(
        ctx.kb_org_id, ctx.kb_name, holding_prefix_for(ctx.id),
    )

    # List the staged files. The prefix uses '/' separators; list_keys
    # returns paths relative to the bucket root.
    keys = await storage_backend.list_keys(bucket, prefix=holding + "/")
    if not keys:
        logger.info(
            "file_upload: no files found under %s for source %s",
            holding, ctx.id,
        )
        return IngestResult()

    existing = await load_existing_raw_docs(
        ctx.kb_id, session_factory=session_factory,
    )
    result = IngestResult()

    for key in sorted(keys):
        filename = key.rsplit("/", 1)[-1]
        if not filename:
            continue

        try:
            stat = await storage_backend.stat(bucket, key)
        except KeyError:
            logger.warning("file_upload: key vanished mid-run: %s", key)
            result.docs_skipped += 1
            continue

        size = int(stat.get("size") or 0)
        if size > max_bytes:
            logger.warning(
                "file_upload: skipping %s (%d bytes > limit %d)",
                filename, size, max_bytes,
            )
            result.docs_skipped += 1
            continue

        try:
            raw_bytes = await storage_backend.read(bucket, key)
        except KeyError:
            result.docs_skipped += 1
            continue

        try:
            md_bytes, title = await _convert_to_markdown(filename, raw_bytes)
        except ValueError as exc:
            logger.warning(
                "file_upload: conversion failed for %s (%s)",
                filename, exc,
            )
            result.docs_skipped += 1
            continue

        if not md_bytes:
            result.docs_skipped += 1
            continue

        bucket_path = _output_path_for(filename)
        await upsert_raw_doc(
            ctx,
            bucket_path=bucket_path,
            data=md_bytes,
            title=title,
            url=None,
            session_factory=session_factory,
            storage=storage,
            existing=existing,
            result=result,
        )

        if not preserve_holding:
            try:
                await storage_backend.delete(bucket, key)
            except Exception:
                logger.debug(
                    "file_upload: best-effort delete of %s failed",
                    key, exc_info=True,
                )

    logger.info("file_upload: ingest complete: %s", result.as_dict())
    return result


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


async def _convert_to_markdown(
    filename: str,
    data: bytes,
) -> tuple[bytes, Optional[str]]:
    """Convert *data* (interpreted via *filename*'s extension) to UTF-8
    markdown bytes via markitdown.

    Markdown inputs (``.md``, ``.markdown``) pass through unchanged so
    we don't waste a round-trip; markitdown would render them
    identically.
    """
    ext = _file_extension(filename)
    if ext in (".md", ".markdown"):
        text = data.decode("utf-8", errors="replace")
        return text.encode("utf-8"), _first_heading(text)

    # markitdown is sync; off-thread it.
    from markitdown import MarkItDown

    converter = MarkItDown()
    try:
        result = await asyncio.to_thread(
            converter.convert_stream,
            io.BytesIO(data),
            file_extension=ext or ".bin",
        )
    except Exception as exc:
        raise ValueError(f"markitdown failed: {exc}") from exc

    md_text = (result.text_content or "").strip()
    if not md_text:
        return b"", None
    title = (
        getattr(result, "title", None) or _first_heading(md_text)
    )
    return md_text.encode("utf-8"), (
        str(title).strip()[:200] if title else None
    )


def _file_extension(filename: str) -> str:
    """Return ``.lower`` extension (with leading dot) from a filename."""
    _, ext = os.path.splitext(filename)
    return ext.lower()


def _first_heading(text: str) -> Optional[str]:
    for line in text.splitlines()[:30]:
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 2:
            return stripped[2:].strip()[:200] or None
    return None


def _output_path_for(filename: str) -> str:
    """Map a holding filename to the raw/ output path.

    ``setup.pdf`` -> ``raw/setup.pdf.md`` (keeps the original
    extension visible so the agent's citation surfaces the source
    format). ``intro.md`` -> ``raw/intro.md`` (no double suffix).
    """
    stem = filename
    if stem.lower().endswith((".md", ".markdown")):
        return f"{_RAW_PREFIX}/{stem}"
    return f"{_RAW_PREFIX}/{stem}.md"
