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

import codecs
import errno
import logging
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from surogates.devices.workspace import DeviceWorkspaceIO

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


async def load_agent_md(bundle) -> str | None:
    """Load AGENT.md (preferred) or AGENTS.md from the bundle.

    Both filenames are common in the wild;
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
        return truncate_context(content, filename=name, readable=False)
    return None


async def load_soul_md(bundle) -> str | None:
    """Load SOUL.md from the agent's file bundle.

    Replaces the legacy asset_root-string
    Bundle-less agents (i.e. ones that haven't been published yet)
    pass ``None`` and the loader returns ``None`` silently so the
    prompt builder skips the SOUL.md section rather than crashing.

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
    return truncate_context(content, filename="SOUL.md", readable=False)


#: The most of a context file read: far past what the prompt keeps
#: (``truncate_context``), and a file made huge (a sparse one) is not read whole.
MAX_CONTEXT_FILE_BYTES = 1 << 20


def read_context_file(path: Path, workspace: str | None) -> str | None:
    """The text of the context file *path* for the prompt, or None.

    Never anything out of the workspace through a link: a link at the file's
    name is followed only to a regular file inside *workspace*; a file whose
    folder, or its name, leads out of the workspace is skipped, with one
    warning naming the file, and its content is never read.  A file in the
    workspace is read through the no-follow walk from the workspace's root
    (``LocalWorkspaceIO``), so a folder swapped for a link meanwhile is
    refused, as is a file with other names elsewhere (a hard link).  A file
    above the workspace (the search up to the repository's root) is opened
    without following its name.  Only a regular file is read, without waiting
    (a FIFO by the name is no context file), and at most
    ``MAX_CONTEXT_FILE_BYTES`` of it; strict UTF-8 (a character the cap cuts
    in two is left out).
    """
    from surogates.tools.workspace_io.local import _BY_FOLDER, _open_to_read

    root = os.path.realpath(workspace) if workspace else None

    def inside(target: str) -> bool:
        if root is None or target == root:
            return False
        try:
            return os.path.commonpath([root, target]) == root
        except ValueError:
            return False

    in_workspace = inside(os.path.abspath(path))

    def shown() -> str:
        return os.path.relpath(os.path.abspath(path), root) if root and in_workspace else path.name

    def out_of_the_workspace() -> None:
        logger.warning("Context file %s leads out of the workspace: skipped, not read", shown())

    try:
        if os.path.islink(path):
            source = os.path.realpath(path)
            if not inside(source):
                out_of_the_workspace()
                return None
        else:
            # Its folder as resolved, so a file in the workspace is read
            # through the walk from the workspace's root.
            source = os.path.join(os.path.realpath(path.parent), path.name)
            if in_workspace and not inside(source):
                out_of_the_workspace()  # a folder on its path is a link out
                return None
        if inside(source) and _BY_FOLDER:
            with _open_to_read(root, source) as fh:
                if os.fstat(fh.fileno()).st_nlink > 1:
                    logger.warning("Context file %s has other names (a hard link): skipped, not read", shown())
                    return None
                data = fh.read(MAX_CONTEXT_FILE_BYTES)
        else:
            fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    return None
                os.set_blocking(fh.fileno(), True)
                data = fh.read(MAX_CONTEXT_FILE_BYTES)
    except FileNotFoundError:
        return None
    except PermissionError:
        # Refused by the walk (a folder on the way, or the name, swapped for a
        # link after the check), or a file this worker may not read.
        logger.warning("Context file %s could not be read: skipped", shown())
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            out_of_the_workspace()
        elif exc.errno not in (errno.ENOTDIR, errno.EISDIR, errno.EINVAL):
            logger.warning("Failed to read context file %s", shown())
        return None
    try:
        return codecs.getincrementaldecoder("utf-8")().decode(data, final=len(data) < MAX_CONTEXT_FILE_BYTES)
    except UnicodeDecodeError:
        return None


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
            text = read_context_file(current / filename, workspace_path)
            content = (text or "").strip()
            if content:
                content = scan_context_content(content, filename)
                return truncate_context(content)

        if current == stop_at:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    return None


async def load_folder_context(files: "DeviceWorkspaceIO") -> str | None:
    """Project context from the top of a local folder, read through its computer.

    The first of :data:`PROJECT_CONTEXT_FILENAMES` there that is UTF-8
    text, scanned and cut as :func:`load_project_context` does on this
    host.  Each name is read in turn, a missing one answered as such: a
    listing of a big folder stops short of its names.  Only the folder's
    top: the computer gives nothing above it.  At most one frame of it is
    read (``MAX_PAYLOAD_BYTES``), far past what the prompt keeps; a
    character the frame cuts in two is left out.
    """
    from surogates.devices.workspace import MAX_PAYLOAD_BYTES
    from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

    for filename in PROJECT_CONTEXT_FILENAMES:
        try:
            data = await files.read(await files.resolve(filename), max_bytes=MAX_PAYLOAD_BYTES)
            # Strict, so the scan reads what the model would: a byte that is
            # not UTF-8 could hide a pattern from it.
            content = codecs.getincrementaldecoder("utf-8")().decode(
                data, final=len(data) < MAX_PAYLOAD_BYTES,
            ).strip()
        except (OSError, WorkspaceSandboxError, UnicodeDecodeError):
            # None by that name, a folder by it, a link out of the folder, a
            # file the computer will not read, or one that is not text.
            continue
        if content:
            return truncate_context(scan_context_content(content, filename))
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
    readable: bool = True,
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
    # A bundle file is not in the workspace: pointing the model at file
    # tools sends it searching for something it can never find.
    hint = " Use file tools to read the full file." if readable else ""
    marker = f"\n\n[...truncated {label}: kept {head_chars}+{tail_chars} of {len(content)} chars.{hint}]\n\n"
    if not readable:
        logger.warning("%s is %d chars, over the %d limit: its middle is cut", label, len(content), max_chars)

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
