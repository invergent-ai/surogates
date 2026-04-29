"""``markdown_dir`` source runner — walks a local directory or git repo
and ingests every matching markdown file as a ``kb_raw_doc``.

Config schema (``kb_source.config``):

  - ``path`` (str): absolute filesystem path to walk. Mutually
    exclusive with ``git_url``. Used by tests + on-prem deployments
    where the ingest worker has the docs mounted.
  - ``git_url`` (str): https git URL to shallow-clone. Optional with
    ``git_ref`` (default: ``HEAD``) and ``git_subdir`` (default: ``""``).
    Worker needs ``git`` on PATH; clone goes to a temp dir and is
    cleaned up after the run.
  - ``glob`` (str): glob pattern relative to the walk root
    (default: ``**/*.md``). Use ``**/*.{md,mdx}`` to include MDX, etc.
  - ``max_bytes_per_doc`` (int): files larger than this are skipped
    (default: 5 MiB).

Idempotency: each file's content is hashed (sha256). On re-run, files
with unchanged hashes are counted as ``docs_unchanged`` and neither
re-uploaded nor re-inserted. Files with new hashes are uploaded and
the existing ``kb_raw_doc`` row is updated.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.jobs.kb_sources._base import IngestResult, SourceContext
from surogates.storage.backend import StorageBackend
from surogates.storage.kb_storage import KbStorage

logger = logging.getLogger(__name__)

KIND = "markdown_dir"

DEFAULT_GLOB = "**/*.md"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB

# Path inside the KB bucket where raw bytes are written:
# ``raw/{document_path}``. The wiki maintainer (step 5) writes its
# compiled outputs to ``wiki/...`` alongside.
_RAW_PREFIX = "raw"


async def run(
    ctx: SourceContext,
    *,
    session_factory: async_sessionmaker,
    storage_backend: StorageBackend,
) -> IngestResult:
    """Ingest a markdown directory into the KB.

    Returns the per-run :class:`IngestResult`. Raises on misconfig
    (missing path, invalid git URL, etc.) so the dispatcher can mark
    the source's ``last_status='failed'`` with the error message.
    """
    config = ctx.config or {}
    glob_pattern = config.get("glob") or DEFAULT_GLOB
    max_bytes = int(config.get("max_bytes_per_doc") or DEFAULT_MAX_BYTES)

    walk_root, cleanup = await _resolve_walk_root(config)

    try:
        return await _walk_and_ingest(
            walk_root,
            glob_pattern,
            max_bytes=max_bytes,
            ctx=ctx,
            session_factory=session_factory,
            storage_backend=storage_backend,
        )
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# Walk + ingest
# ---------------------------------------------------------------------------


async def _walk_and_ingest(
    root: Path,
    glob_pattern: str,
    *,
    max_bytes: int,
    ctx: SourceContext,
    session_factory: async_sessionmaker,
    storage_backend: StorageBackend,
) -> IngestResult:
    if not root.exists():
        raise FileNotFoundError(f"markdown_dir path does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"markdown_dir path is not a directory: {root}")

    storage = KbStorage(storage_backend)
    result = IngestResult()

    # Snapshot of existing rows so we can decide add vs. update vs.
    # unchanged in a single pass without round-tripping per file.
    async with session_factory() as db:
        existing_rows = (
            await db.execute(
                text(
                    "SELECT id, path, content_sha "
                    "FROM kb_raw_doc WHERE kb_id = :kb_id"
                ),
                {"kb_id": ctx.kb_id},
            )
        ).all()
    existing: dict[str, tuple[uuid.UUID, str]] = {
        r.path: (r.id, r.content_sha) for r in existing_rows
    }

    files = sorted(p for p in root.glob(glob_pattern) if p.is_file())
    logger.info(
        "markdown_dir: walking %s (%d candidates)", root, len(files),
    )

    for f in files:
        rel = str(f.relative_to(root)).replace(os.sep, "/")
        bucket_path = f"{_RAW_PREFIX}/{rel}"

        try:
            stat = f.stat()
        except OSError as exc:
            logger.warning("markdown_dir: cannot stat %s (%s)", f, exc)
            result.docs_skipped += 1
            continue
        if stat.st_size > max_bytes:
            logger.warning(
                "markdown_dir: skipping %s (%d bytes > limit %d)",
                f, stat.st_size, max_bytes,
            )
            result.docs_skipped += 1
            continue

        try:
            data = f.read_bytes()
        except OSError as exc:
            logger.warning("markdown_dir: cannot read %s (%s)", f, exc)
            result.docs_skipped += 1
            continue

        sha = hashlib.sha256(data).hexdigest()
        title = _infer_title(data)

        prior = existing.get(bucket_path)
        if prior is None:
            # New doc.
            await storage.write_entry(
                ctx.kb_org_id, ctx.kb_name, bucket_path, data,
            )
            async with session_factory() as db:
                await db.execute(
                    text(
                        "INSERT INTO kb_raw_doc "
                        "(id, kb_id, source_id, path, content_sha, title) "
                        "VALUES (:id, :kb_id, :source_id, :path, :sha, :title)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "kb_id": ctx.kb_id,
                        "source_id": ctx.id,
                        "path": bucket_path,
                        "sha": sha,
                        "title": title,
                    },
                )
                await db.commit()
            result.docs_added += 1
            result.bytes_written += len(data)
        elif prior[1] != sha:
            # Changed doc.
            await storage.write_entry(
                ctx.kb_org_id, ctx.kb_name, bucket_path, data,
            )
            async with session_factory() as db:
                await db.execute(
                    text(
                        "UPDATE kb_raw_doc "
                        "SET content_sha = :sha, "
                        "    title = :title, "
                        "    ingested_at = NOW() "
                        "WHERE id = :id"
                    ),
                    {"sha": sha, "title": title, "id": prior[0]},
                )
                await db.commit()
            result.docs_updated += 1
            result.bytes_written += len(data)
        else:
            # Unchanged — skip both upload and DB update.
            result.docs_unchanged += 1

    logger.info("markdown_dir: ingest complete: %s", result.as_dict())
    return result


# ---------------------------------------------------------------------------
# Title inference + git clone helpers
# ---------------------------------------------------------------------------


def _infer_title(data: bytes) -> Optional[str]:
    """Pull the first ATX-style ``# Heading`` from the file body.

    Returns ``None`` for binary files or files without a leading
    heading. Trims trailing whitespace; max 200 chars to bound
    pathological inputs.
    """
    try:
        text_data = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for line in text_data.splitlines()[:30]:
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 2:
            return stripped[2:].strip()[:200] or None
    return None


async def _resolve_walk_root(
    config: dict,
) -> Tuple[Path, Callable[[], None]]:
    """Return ``(walk_root, cleanup_fn)``.

    For local-path sources, ``cleanup_fn`` is a no-op. For git sources,
    a shallow clone is created in a tmp dir and ``cleanup_fn`` removes
    it.
    """
    path = config.get("path")
    git_url = config.get("git_url")

    if path and git_url:
        raise ValueError(
            "markdown_dir source: 'path' and 'git_url' are mutually "
            "exclusive"
        )
    if path:
        return Path(path).resolve(), lambda: None
    if git_url:
        return await _git_clone(
            git_url,
            ref=config.get("git_ref") or "HEAD",
            subdir=config.get("git_subdir") or "",
        )
    raise ValueError(
        "markdown_dir source requires either 'path' or 'git_url' in config"
    )


async def _git_clone(
    url: str,
    ref: str,
    subdir: str,
) -> Tuple[Path, Callable[[], None]]:
    """Shallow-clone *url* @ *ref* to a tmp dir; return ``(root, cleanup)``.

    Uses ``git`` from PATH. The clone is depth=1; for non-HEAD refs
    we clone the default branch then ``git checkout`` because
    ``--branch`` doesn't accept arbitrary refs.
    """
    tmp = Path(tempfile.mkdtemp(prefix="kb_md_dir_"))

    def cleanup() -> None:
        shutil.rmtree(tmp, ignore_errors=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", url, str(tmp),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            cleanup()
            raise RuntimeError(
                f"git clone failed for {url!r}: "
                f"{stderr.decode(errors='replace').strip()}"
            )

        if ref and ref != "HEAD":
            # Fetch + checkout the requested ref. Depth=1 again to keep
            # the clone small; works for most branches and tags.
            proc2 = await asyncio.create_subprocess_exec(
                "git", "-C", str(tmp), "fetch", "--depth", "1", "origin", ref,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc2.communicate()
            if proc2.returncode != 0:
                cleanup()
                raise RuntimeError(
                    f"git fetch failed for {url!r} @ {ref!r}: "
                    f"{stderr.decode(errors='replace').strip()}"
                )
            proc3 = await asyncio.create_subprocess_exec(
                "git", "-C", str(tmp), "checkout", "FETCH_HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc3.communicate()
            if proc3.returncode != 0:
                cleanup()
                raise RuntimeError(
                    f"git checkout failed for {url!r} @ {ref!r}: "
                    f"{stderr.decode(errors='replace').strip()}"
                )
    except Exception:
        cleanup()
        raise

    target = tmp / subdir if subdir else tmp
    return target, cleanup
