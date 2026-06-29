"""Outbound MEDIA: marker handling for channel replies.

The channel agent shares a workspace file by emitting ``MEDIA:<path>`` in its
reply text. This module parses those markers out of the text (so the raw marker
never reaches the channel) and reads the referenced bytes from the session
workspace storage backend, ready for a platform to upload.

Workspace files only: ``MEDIA:/workspace/<rel>``, ``MEDIA:workspace/<rel>``,
``MEDIA:/<rel>``, and ``MEDIA:<rel>`` all resolve to the session-workspace
relative path ``<rel>``. Paths that escape the workspace (``..``) are rejected.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from surogates.session.attachment_ingest import workspace_root_id
from surogates.storage.tenant import prefixed_session_workspace_key

logger = logging.getLogger(__name__)

__all__ = [
    "OutboundFile",
    "parse_media_markers",
    "normalize_workspace_path",
    "resolve_workspace_media",
]

# Optional surrounding backtick/quote + MEDIA: + optional whitespace + a path of
# non-whitespace, non-quote characters + optional closing backtick/quote. Mirrors
# hermes's stream_consumer regex; paths with spaces are out of scope.
_MEDIA_RE = re.compile(r"""[`"']?MEDIA:\s*([^\s`"']+)[`"']?""")


@dataclass
class OutboundFile:
    """A workspace file resolved from a MEDIA: marker, ready to upload."""

    filename: str
    mime_type: str
    data: bytes


def parse_media_markers(text: str) -> tuple[list[str], str]:
    """Return ``(raw_paths, cleaned_text)`` for *text*.

    ``raw_paths`` are the marker paths in order of appearance. ``cleaned_text``
    is *text* with every marker (and its surrounding backtick/quote) removed and
    surrounding whitespace tidied. Text without a marker is returned unchanged.
    """
    if "MEDIA:" not in (text or ""):
        return [], text or ""
    paths = [m.group(1) for m in _MEDIA_RE.finditer(text)]
    cleaned = _MEDIA_RE.sub("", text)
    # Collapse the runs of spaces/tabs a mid-sentence removal can leave behind,
    # without touching newlines, then trim the ends.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return paths, cleaned


def normalize_workspace_path(raw: str) -> str | None:
    """Normalize a marker path to a workspace-relative POSIX path, or ``None``.

    Strips a leading ``/workspace/`` or ``workspace/`` then a leading ``/``.
    Returns ``None`` for empty paths or paths that escape the workspace (``..``).
    """
    s = (raw or "").strip().strip("`\"'").strip()
    for prefix in ("/workspace/", "workspace/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    else:
        s = s.lstrip("/")
    s = s.strip()
    if not s:
        return None
    p = PurePosixPath(s)
    if p.is_absolute() or any(part == ".." for part in p.parts):
        return None
    norm = p.as_posix()
    if not norm or norm == ".":
        return None
    return norm


async def resolve_workspace_media(
    storage: Any,
    session: Any,
    *,
    paths: list[str],
    max_files: int,
    max_bytes: int,
) -> list[OutboundFile]:
    """Read workspace files referenced by *paths* from *storage*.

    Best-effort: a path that is malformed, missing, over ``max_bytes``, or
    unreadable is skipped and logged. At most ``max_files`` paths are considered.
    An empty ``storage_bucket`` on the session yields no files.
    """
    bucket = (getattr(session, "config", None) or {}).get("storage_bucket") or ""
    if not bucket:
        logger.warning("[channel_media] session has no storage_bucket; skipping %d file(s)", len(paths))
        return []
    root_id = workspace_root_id(session)
    out: list[OutboundFile] = []
    for raw in paths[:max_files]:
        rel = normalize_workspace_path(raw)
        if rel is None:
            logger.warning("[channel_media] rejecting malformed media path")
            continue
        key = prefixed_session_workspace_key(session.config, root_id, rel)
        try:
            meta = await storage.stat(bucket, key)
        except KeyError:
            logger.warning("[channel_media] media file not found: %s", rel)
            continue
        except Exception:
            logger.warning("[channel_media] stat failed for %s", rel, exc_info=True)
            continue
        if int(meta.get("size", 0)) > max_bytes:
            logger.warning("[channel_media] media file over cap: %s", rel)
            continue
        try:
            data = await storage.read(bucket, key)
        except Exception:
            logger.warning("[channel_media] read failed for %s", rel, exc_info=True)
            continue
        filename = PurePosixPath(rel).name
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out.append(OutboundFile(filename=filename, mime_type=mime, data=data))
    return out
