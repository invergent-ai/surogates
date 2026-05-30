"""Context file discovery -- loads project-level instruction files.

Discovers and loads context files from the workspace/sandbox:
- SOUL.md -- agent identity (from tenant asset root)
- AGENTS.md -- project-level instructions
- CLAUDE.md -- project-level instructions (alternative)
- .cursorrules -- IDE-compatible rules

Priority: AGENTS.md > CLAUDE.md > .cursorrules (first match wins).
SOUL.md is loaded independently (always, from asset root).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Context file priority (first match wins for project context).
PROJECT_CONTEXT_FILENAMES: list[str] = [
    "AGENTS.md",
    "agents.md",
    "CLAUDE.md",
    "claude.md",
    ".cursorrules",
]

MAX_CONTEXT_CHARS: int = 20_000
_TRUNCATE_HEAD_RATIO: float = 0.70
_TRUNCATE_TAIL_RATIO: float = 0.20

# ---------------------------------------------------------------------------
# Injection scanning patterns
# ---------------------------------------------------------------------------

_CONTEXT_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->', "html_comment_injection"),
    (r'<\s*div\s+style\s*=\s*["\'].*display\s*:\s*none', "hidden_div"),
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)', "translate_execute"),
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)', "read_secrets"),
]

_CONTEXT_INVISIBLE_CHARS: frozenset[str] = frozenset({
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_soul_md_from_disk(asset_root: str) -> str | None:
    """Load SOUL.md from tenant asset root (legacy helm path).

    Plan 3 / Task 10.  Kept for the helm-mode legacy path where the
    agent's SOUL.md lives on the tenant's PVC.  Shared-runtime
    callers should use the async :func:`load_soul_md(bundle)`
    instead.  Plan 9 retires this disk-reading variant entirely.

    Checks ``{asset_root}/shared/SOUL.md`` and ``{asset_root}/SOUL.md``.
    """
    root = Path(asset_root)
    for candidate in (root / "shared" / "SOUL.md", root / "SOUL.md"):
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                content = scan_context_content(content, "SOUL.md")
                content = truncate_context(content)
                return content
            except OSError:
                logger.warning("Failed to read %s", candidate)
    return None


async def load_agent_md(bundle) -> str | None:
    """Load AGENT.md (preferred) or AGENTS.md from the bundle.

    Plan 3 / Task 11.  Both filenames are common in the wild;
    AGENT.md (singular) wins as the modern convention.  Falls back
    to AGENTS.md so legacy bundles still load.

    Returns None on bundle=None OR when neither file exists in the
    bundle.  Content is run through the same injection scan +
    truncation pipeline as SOUL.md — AGENT.md content lands in the
    LLM's system prompt and must be sanitised.
    """
    if bundle is None:
        return None
    for name in ("AGENT.md", "AGENTS.md"):
        try:
            raw = await bundle.read_text(name)
        except LookupError:
            continue
        content = raw.strip()
        if not content:
            continue
        content = scan_context_content(content, name)
        return truncate_context(content)
    return None


async def load_soul_md(bundle) -> str | None:
    """Load SOUL.md from the agent's file bundle.

    Plan 3 / Task 10.  Replaces the legacy asset_root-string
    signature.  Bundle-less agents (helm mode, or an agent that
    hasn't been onboarded to a bundle yet) pass None and the loader
    returns None silently so the prompt builder skips the SOUL.md
    section rather than crashing.

    Runs the loaded content through the same injection scan +
    truncation pipeline as the legacy disk path — a malicious
    tenant bundle must not be able to smuggle prompt injection past
    the LLM's system prompt.
    """
    if bundle is None:
        return None
    try:
        raw = await bundle.read_text("SOUL.md")
    except LookupError:
        return None
    content = raw.strip()
    if not content:
        return None
    content = scan_context_content(content, "SOUL.md")
    return truncate_context(content)


def load_project_context(workspace_path: str | None) -> str | None:
    """Discover and load project context from workspace.

    Walks up to git root if possible, checking each directory for
    :data:`PROJECT_CONTEXT_FILENAMES` in priority order.
    """
    if workspace_path is None:
        return None

    start = Path(workspace_path).resolve()
    git_root = _find_git_root(start)
    stop_at = git_root or start

    current = start
    for _ in range(20):  # safety limit
        for filename in PROJECT_CONTEXT_FILENAMES:
            candidate = current / filename
            if candidate.is_file():
                try:
                    content = candidate.read_text(encoding="utf-8").strip()
                    if not content:
                        continue
                    content = scan_context_content(content, filename)
                    return truncate_context(content)
                except OSError:
                    logger.warning("Failed to read %s", candidate)
                    continue

        if current == stop_at:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    return None


def scan_context_content(content: str, filename: str) -> str:
    """Scan context file content for injection. Returns sanitized content.

    When threats are detected the original content is replaced with a
    ``[BLOCKED: ...]`` marker string so that callers never need to handle ``None``.
    """
    findings: list[str] = []

    for char in _CONTEXT_INVISIBLE_CHARS:
        if char in content:
            findings.append(f"invisible unicode U+{ord(char):04X}")

    for pattern, pid in _CONTEXT_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            findings.append(pid)

    if findings:
        logger.warning(
            "Context file %s blocked: %s", filename, ", ".join(findings)
        )
        return f"[BLOCKED: {filename} contained potential prompt injection ({', '.join(findings)}). Content not loaded.]"

    return content


def truncate_context(
    content: str,
    max_chars: int = MAX_CONTEXT_CHARS,
    filename: str = "",
) -> str:
    """Head/tail truncation with a marker in the middle.

    Uses 70% head + 20% tail strategy.
    """
    if len(content) <= max_chars:
        return content

    head_chars = int(max_chars * _TRUNCATE_HEAD_RATIO)
    tail_chars = int(max_chars * _TRUNCATE_TAIL_RATIO)

    head = content[:head_chars]
    tail = content[-tail_chars:]
    label = filename or "content"
    marker = f"\n\n[...truncated {label}: kept {head_chars}+{tail_chars} of {len(content)} chars. Use file tools to read the full file.]\n\n"

    return head + marker + tail


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _find_git_root(start: Path) -> Path | None:
    """Walk *start* and its parents looking for a ``.git`` directory."""
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None
