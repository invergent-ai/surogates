"""File-backed LRU cache for parsed documents.

The K8s sandbox spawns a fresh ``tool-executor`` Python process per
tool call, so an in-memory cache would be empty on every subsequent
``read_file``.  Persisting to ``/tmp`` (pod-local) survives across
exec calls within the same pod while staying out of the user
workspace, and a cross-process ``fcntl`` lock keeps concurrent
executors from racing on the same entry.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_CACHE_ROOT = Path("/tmp/surogates-read-cache/documents")
DEFAULT_MAX_ENTRIES = 8
DEFAULT_MAX_ENTRY_BYTES = 2 * 1024 * 1024  # 2 MB


class DocumentCache:
    """LRU keyed on ``(abs_path, mtime_ns, size, ext)``.

    Eviction policy: at insert time, if the directory holds more than
    ``max_entries`` cache files, the entries with the oldest atime are
    deleted.  Reads bump the cache file's atime via ``os.utime`` so LRU
    ordering works on filesystems that mount with ``noatime``.
    """

    def __init__(
        self,
        root: Path = DEFAULT_CACHE_ROOT,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    ) -> None:
        self._root = root
        self._max_entries = max_entries
        self._max_entry_bytes = max_entry_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    def _key(self, source: Path) -> str:
        st = source.stat()
        ext = source.suffix.lower()
        raw = f"{source.resolve()}|{st.st_mtime_ns}|{st.st_size}|{ext}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _entry_path(self, key: str) -> Path:
        return self._root / f"{key}.md"

    def _lock_path(self, key: str) -> Path:
        return self._root / f"{key}.lock"

    async def get_or_parse(
        self,
        source: Path,
        parse: Callable[[Path], Awaitable[str]],
    ) -> str:
        """Return cached markdown for ``source`` or call ``parse`` and store.

        If the source can't be stat-ed (missing, permission denied) the
        cache transparently falls through to ``parse(source)`` and lets
        the parser surface its own error.
        """
        try:
            key = self._key(source)
        except OSError as exc:
            logger.debug("cache key stat failed for %s: %s", source, exc)
            return await parse(source)

        entry = self._entry_path(key)
        if entry.exists():
            try:
                content = entry.read_text(encoding="utf-8")
                # Bump atime so LRU eviction prefers other entries.
                now = entry.stat().st_mtime
                os.utime(entry, (os.path.getatime(entry), now))
                return content
            except OSError as exc:
                logger.debug("cache read failed for %s: %s", entry, exc)

        # Miss — parse and persist if small enough.  We deliberately do
        # NOT take a global lock here: an earlier version held an
        # asyncio.Lock around the parse to deduplicate concurrent
        # requests for the same file, but that lock also serialised
        # parses of *different* files, which starved the API event
        # loop when a user sent multiple attachments at once.  Same-key
        # races are rare in practice (the cache file appears the
        # moment the first parse finishes) and harmless when they do
        # happen — the second parse just overwrites the first via the
        # fcntl-locked rename in ``_maybe_store``.
        markdown = await parse(source)
        self._maybe_store(key, markdown)
        return markdown

    def _maybe_store(self, key: str, markdown: str) -> None:
        encoded = markdown.encode("utf-8")
        if len(encoded) > self._max_entry_bytes:
            logger.debug(
                "skipping cache write for %s: %d bytes > limit %d",
                key, len(encoded), self._max_entry_bytes,
            )
            return

        entry = self._entry_path(key)
        tmp = entry.with_suffix(".tmp")
        lock_file = self._lock_path(key)

        # Best-effort cross-process lock.  We hold a file lock only
        # while the rename happens, not while parsing.
        try:
            with open(lock_file, "w") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    tmp.write_bytes(encoded)
                    os.replace(tmp, entry)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            logger.debug("cache write failed for %s: %s", entry, exc)
            return

        self._evict_if_full()

    def _evict_if_full(self) -> None:
        entries = sorted(
            (p for p in self._root.iterdir() if p.suffix == ".md"),
            key=lambda p: p.stat().st_atime,
        )
        for old in entries[: max(0, len(entries) - self._max_entries)]:
            try:
                old.unlink()
            except OSError:
                pass


_DEFAULT: DocumentCache | None = None


def default_cache() -> DocumentCache:
    """Return the process-wide default cache, creating it lazily."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = DocumentCache()
    return _DEFAULT
