"""Artifact promotion and workspace-scan helpers for the harness loop."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

def _coerce_modified_to_datetime(raw: Any) -> "datetime | None":
    """Normalize a storage backend's ``modified`` field to ``datetime``.

    LocalBackend returns a POSIX float (``st_mtime``); S3Backend
    returns the boto3 ``LastModified`` ``datetime`` directly. Anything
    else is treated as unparseable and yields ``None`` so the caller
    skips the entry rather than crashing.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _coerce_tool_args(raw: Any) -> dict[str, Any]:
    """Best-effort coercion of a TOOL_CALL ``arguments`` field to a dict.

    Different tool emitters store ``arguments`` either as a JSON string
    (OpenAI convention) or as a pre-parsed dict.  Anything else is
    treated as opaque and yields an empty dict so candidate-artifact
    collection can keep going.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}
_PROMOTABLE_FENCES: dict[str, tuple[str, str]] = {
    "svg": ("svg", "svg"),
    "html": ("html", "html"),
}

# Precompiled regex that matches ``` + language-tag + body + ``` .  The
# (?s) flag lets ``.`` match newlines inside the body.  Only matches
# fences starting at line-begin to avoid misfires on inline backticks.
_FENCE_RE = re.compile(
    r"(?ms)^```([a-zA-Z0-9_-]+)\s*\n(.*?)^```\s*$"
)
def _derive_artifact_name(kind: str, messages: list[dict]) -> str:
    """Pick a human-readable name for an auto-promoted artifact.

    Uses the most recent user message's first line (trimmed to a
    reasonable length) so the artifact header says something like
    "Draw a minimal SVG logo…" instead of a generic "SVG artifact".
    Falls back to a kind-based default when no user message is
    available or the extract is empty.
    """
    fallback = {
        "svg": "SVG artifact",
        "html": "HTML preview",
    }.get(kind, "Artifact")

    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content") or ""
        if not isinstance(content, str):
            continue
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        # Strip surrounding quotes the frontend sometimes inherits from
        # copy-pasted prompts.
        first_line = first_line.strip(' "\'')
        if first_line:
            return first_line[:80]
        break
    return fallback
