"""Shared attachment-ingest helpers.

Extracted from ``surogates.api.routes.sessions`` so the Slack bridge and other
channels can reuse the same inline-parsing logic without importing the API
router.  The API route re-exports every name from this module for backwards
compatibility.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re as _re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from surogates.storage.tenant import boundary_workspace_key

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Filename sanitization (prompt-injection guard)
# ---------------------------------------------------------------------------

_CONTROL_CHARS_RE = _re.compile(r"[\x00-\x1f\x7f]")


def safe_display_name(name: str, *, max_len: int = 100) -> str:
    """Filename sanitized for inclusion in model-visible text: control chars
    (incl. newlines/tabs) collapsed to spaces, whitespace squeezed, truncated.
    Defends against prompt-injection via crafted filenames regardless of any
    detector."""
    cleaned = _CONTROL_CHARS_RE.sub(" ", name or "")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned or "file"


# ---------------------------------------------------------------------------
# Shared injection-detector singleton (decoupled from the API router)
# ---------------------------------------------------------------------------

_injection_detector = None


def get_injection_detector():
    """Return the process-wide PromptInjectionDetector, created on first call."""
    global _injection_detector
    if _injection_detector is None:
        from agent_os.prompt_injection import PromptInjectionDetector
        _injection_detector = PromptInjectionDetector()
    return _injection_detector

# ---------------------------------------------------------------------------
# Inline-attachment parameters
# ---------------------------------------------------------------------------

_INLINE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB raw cap for inline parsing
_INLINE_RENDERED_CAP_CHARS = 200_000  # 200 KB of rendered text/markdown
_INLINE_TOTAL_RENDERED_CAP_CHARS = int(
    os.environ.get("SUROGATES_INLINE_TOTAL_RENDERED_CAP_CHARS", "50000"),
)

_INLINE_DOC_EXTS = frozenset({".pdf", ".docx", ".xlsx", ".pptx"})
_INLINE_TEXT_EXTS = frozenset({
    ".txt", ".md", ".json", ".csv", ".tsv",
    ".yaml", ".yml", ".log",
})


def _apply_inline_total_budget(
    parse_outcomes: list[tuple[str | None, str | None, str | None]],
    budget: int = _INLINE_TOTAL_RENDERED_CAP_CHARS,
) -> list[tuple[str | None, str | None, str | None]]:
    """Enforce the per-message inlined-text budget across attachments.

    Walks ``parse_outcomes`` (one tuple per inline-candidate attachment,
    in submission order, of ``(inlined_text, inlined_kind,
    skip_reason)``) and returns the same list demoted by the budget:
    once the running total of ``len(inlined_text)`` would exceed
    ``budget``, every subsequent successful parse is dropped and tagged
    ``inline_skip_reason="total_budget_exceeded"``.  Failed parses
    (``inlined_text=None``) and pre-existing skip reasons are preserved
    untouched.

    Pure function — extracted so the budget policy can be unit-tested
    without spinning up the full send-message route.
    """
    out: list[tuple[str | None, str | None, str | None]] = []
    used = 0
    over_budget = False
    for inlined_text, inlined_kind, skip_reason in parse_outcomes:
        if inlined_text is None:
            out.append((None, None, skip_reason))
            continue
        if over_budget:
            # First-overflow-stops: once a single successful parse
            # would push us past the cap, every later successful parse
            # is demoted too — deterministic and order-respecting, vs.
            # greedy packing which can fragmentally keep tiny tail
            # files while dropping a useful middle one.
            out.append((None, None, "total_budget_exceeded"))
            continue
        chars = len(inlined_text)
        if used + chars > budget:
            over_budget = True
            out.append((None, None, "total_budget_exceeded"))
            continue
        used += chars
        out.append((inlined_text, inlined_kind, None))
    return out


def _inline_extension_kind(filename: str) -> Literal["document", "text"] | None:
    """Map a filename to its inline-parsing kind, or None if unsupported."""
    ext = os.path.splitext(filename)[1].lower()
    if not ext:
        return None
    if ext in _INLINE_DOC_EXTS:
        return "document"
    if ext in _INLINE_TEXT_EXTS:
        return "text"
    return None


_INLINE_MATERIALIZE_ROOT = Path("/tmp/surogates-attachment-inline")


def _materialize_for_cache(
    raw_bytes: bytes,
    *,
    bucket: str,
    storage_key: str,
    size: int,
    modified: str,
    suffix: str,
    cache_root: Path = _INLINE_MATERIALIZE_ROOT,
) -> Path:
    """Write ``raw_bytes`` to a deterministic temp file keyed on identity.

    The document cache hashes the source path's
    ``(absolute_path, mtime_ns, size, ext)`` tuple.  By materialising
    the bytes once into a deterministic location, re-sending the same
    attachment within a pod's lifetime hits the cache instead of
    re-parsing.

    The filename embeds a SHA-256 of (bucket, storage_key, size,
    modified) so distinct uploads never collide and re-uploads with
    different bytes get a fresh entry.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(
        f"{bucket}|{storage_key}|{size}|{modified}".encode("utf-8"),
    ).hexdigest()
    target = cache_root / f"{fingerprint}{suffix.lower()}"
    if not target.exists():
        tmp_file = tempfile.NamedTemporaryFile(
            dir=cache_root,
            prefix=f"{fingerprint}.",
            suffix=".part",
            delete=False,
        )
        tmp = Path(tmp_file.name)
        try:
            with tmp_file:
                tmp_file.write(raw_bytes)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
    return target


async def _try_inline_attachment(
    attachment: Any,
    raw_bytes: bytes,
    document_path: Path | None,
) -> tuple[str | None, str | None, str | None]:
    """Decide whether to inline ``attachment`` and return the result.

    Returns ``(inlined_text, inlined_render_kind, inline_skip_reason)``.
    The first two are populated on success; the third is populated when
    a *supported* attachment was considered but skipped, so the prompt
    note can explain the fallback to the agent.  All three are ``None``
    when the file is silently out of scope (over the raw cap or
    unsupported extension) -- there is nothing useful to tell the LLM.
    """
    if attachment.size is not None and attachment.size > _INLINE_MAX_BYTES:
        return None, None, None
    kind = _inline_extension_kind(attachment.filename)
    if kind is None:
        return None, None, None

    if kind == "document":
        if document_path is None:
            return None, None, "parse_error"
        from surogates.tools.builtin.file_ops import (  # noqa: PLC0415
            DocumentParseError,
            _parse_document_to_text,
        )
        from surogates.tools.utils.document_cache import (  # noqa: PLC0415
            default_cache,
        )

        try:
            md = await default_cache().get_or_parse(
                document_path, _parse_document_to_text,
            )
        except DocumentParseError as exc:
            reason = (
                "parse_timeout"
                if "timeout" in exc.reason.lower()
                else "parse_error"
            )
            logger.info(
                "event=attachment.inline result=skip reason=%s "
                "filename=%s err=%s",
                reason, attachment.filename, exc.reason,
            )
            return None, None, reason
        if not md.strip():
            return None, None, "empty_output"
        if len(md) > _INLINE_RENDERED_CAP_CHARS:
            logger.info(
                "event=attachment.inline result=skip reason=oversize_output "
                "filename=%s chars=%d",
                attachment.filename, len(md),
            )
            return None, None, "oversize_output"
        return md, "markdown", None

    # kind == "text"
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "decode_error"
    if len(text) > _INLINE_RENDERED_CAP_CHARS:
        return None, None, "oversize_output"
    return text, "text", None


# ---------------------------------------------------------------------------
# Public helpers for non-API callers (e.g. the Slack bridge)
# ---------------------------------------------------------------------------


def _is_image_mime(mime_type: str) -> bool:
    return (mime_type or "").lower().startswith("image/")


# Raster image formats that can be shown to a model as a vision block or read by
# vision tools. SVG/TIFF/BMP are image/* but not natively renderable, so they are
# treated as plain attachments rather than viewable images.
RASTER_IMAGE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
})


def is_raster_image_mime(mime_type: str) -> bool:
    return (mime_type or "").lower().split(";")[0].strip() in RASTER_IMAGE_MIMES


def workspace_root_id(session: Any) -> str:
    """Return the workspace root session id for *session*.

    When the session was spawned inside a sandbox whose root is a
    different session (``sandbox_root_session_id`` in config), the
    workspace root is that ancestor — all sub-sessions share the same
    directory tree rooted at the ancestor's id.  Falls back to
    ``session.id`` for top-level sessions.
    """
    root = (getattr(session, "config", None) or {}).get("sandbox_root_session_id")
    return str(root) if root else str(session.id)


async def ingest_attachment_bytes(
    storage: Any,
    *,
    session: Any,
    root_id: str,
    bucket: str,
    path: str,
    filename: str,
    mime_type: str,
    data: bytes,
    inline_images: bool = True,
) -> dict:
    """Turn downloaded bytes into an ``images[]`` or ``attachments[]`` event
    entry, mirroring the API upload route. Images return an image entry and are
    NOT written to the workspace. Non-images are written to the session
    workspace and return an attachment entry with the same inline rules as the
    API route.

    When *inline_images* is False, images are written to the workspace instead
    of being returned as base64 blobs, and an attachment entry (with path) is
    returned instead of an image entry. The caller is responsible for
    distinguishing images from other attachments via the mime_type.
    """
    if _is_image_mime(mime_type) and inline_images:
        return {"image": {"data": base64.b64encode(data).decode(), "mime_type": mime_type}}

    key = boundary_workspace_key(session.config, session, root_id, path)
    await storage.write(bucket, key, data)

    entry: dict = {
        "path": path, "filename": filename, "mime_type": mime_type, "size": len(data),
    }
    # Per-file inline only. The cross-file total-inline budget
    # (_apply_inline_total_budget) is applied by the batch caller over
    # all of a message's attachments — do NOT pre-apply it here.
    ref = SimpleNamespace(filename=filename, mime_type=mime_type, size=len(data), path=path)
    document_path = None
    if _inline_extension_kind(filename) == "document":
        suffix = os.path.splitext(filename)[1].lower()
        document_path = _materialize_for_cache(
            raw_bytes=data, bucket=bucket, storage_key=key,
            size=len(data), modified="", suffix=suffix,
        )
    inlined_text, inlined_kind, skip_reason = await _try_inline_attachment(ref, data, document_path)
    if inlined_text is not None:
        entry["inlined_text"] = inlined_text
        entry["inlined_render_kind"] = inlined_kind
    elif skip_reason is not None:
        entry["inline_skip_reason"] = skip_reason
    return {"attachment": entry}
