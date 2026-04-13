"""Builtin file operation tools -- read_file, write_file, patch, search_files, list_files.

These tools are classified as sandbox tools: in production the
:class:`ToolRouter` forwards them to the sandbox runtime.  The handlers
here serve as harness-local fallbacks for development and testing.
"""

from __future__ import annotations

import difflib
import errno
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXPECTED_WRITE_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}

# ---------------------------------------------------------------------------
# Write-path deny list — blocks writes to sensitive system/credential files
# ---------------------------------------------------------------------------

_HOME = str(Path.home())

WRITE_DENIED_PATHS = {
    os.path.realpath(p) for p in [
        os.path.join(_HOME, ".ssh", "authorized_keys"),
        os.path.join(_HOME, ".ssh", "id_rsa"),
        os.path.join(_HOME, ".ssh", "id_ed25519"),
        os.path.join(_HOME, ".ssh", "config"),
        os.path.join(_HOME, ".bashrc"),
        os.path.join(_HOME, ".zshrc"),
        os.path.join(_HOME, ".profile"),
        os.path.join(_HOME, ".bash_profile"),
        os.path.join(_HOME, ".zprofile"),
        os.path.join(_HOME, ".netrc"),
        os.path.join(_HOME, ".pgpass"),
        os.path.join(_HOME, ".npmrc"),
        os.path.join(_HOME, ".pypirc"),
        "/etc/sudoers",
        "/etc/passwd",
        "/etc/shadow",
    ]
}

WRITE_DENIED_PREFIXES = [
    os.path.realpath(p) + os.sep for p in [
        os.path.join(_HOME, ".ssh"),
        os.path.join(_HOME, ".aws"),
        os.path.join(_HOME, ".gnupg"),
        os.path.join(_HOME, ".kube"),
        "/etc/sudoers.d",
        "/etc/systemd",
        os.path.join(_HOME, ".docker"),
        os.path.join(_HOME, ".azure"),
        os.path.join(_HOME, ".config", "gh"),
    ]
]


def _get_safe_write_root() -> str | None:
    """Return the resolved SUROGATES_WRITE_SAFE_ROOT path, or None if unset.

    When set, all write_file/patch operations are constrained to this
    directory tree.  Writes outside it are denied even if the target is
    not on the static deny list.  Opt-in hardening for gateway/messaging
    deployments that should only touch a workspace checkout.
    """
    root = os.getenv("SUROGATES_WRITE_SAFE_ROOT", "")
    if not root:
        return None
    try:
        return os.path.realpath(os.path.expanduser(root))
    except Exception:
        return None


def _is_write_denied(path: str) -> bool:
    """Return True if path is on the write deny list.

    Checks the static deny list of sensitive system/credential files,
    then the optional safe-root sandbox (SUROGATES_WRITE_SAFE_ROOT).
    """
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    # 1) Static deny list
    if resolved in WRITE_DENIED_PATHS:
        return True
    for prefix in WRITE_DENIED_PREFIXES:
        if resolved.startswith(prefix):
            return True

    # 2) Optional safe-root sandbox
    safe_root = _get_safe_write_root()
    if safe_root:
        if not (resolved == safe_root or resolved.startswith(safe_root + os.sep)):
            return True

    return False


# ---------------------------------------------------------------------------
# Image extensions — subset of binary that we can redirect to vision tools
# ---------------------------------------------------------------------------
IMAGE_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico',
})


def _is_image(path: str) -> bool:
    """Check if file is an image by extension."""
    ext = os.path.splitext(path)[1].lower()
    return ext in IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# Linters by file extension — run syntax check after write/patch
# ---------------------------------------------------------------------------
LINTERS = {
    '.py': 'python -m py_compile {file} 2>&1',
    '.js': 'node --check {file} 2>&1',
    '.ts': 'npx tsc --noEmit {file} 2>&1',
    '.go': 'go vet {file} 2>&1',
    '.rs': 'rustfmt --check {file} 2>&1',
}


def _check_lint(filepath: str) -> dict[str, Any] | None:
    """Run syntax check on a file after editing.

    Returns a dict with lint status, or None if no linter is available
    for this file type.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in LINTERS:
        return None

    linter_template = LINTERS[ext]
    # Extract the base command (first word) and check availability
    base_cmd = linter_template.split()[0]
    if not shutil.which(base_cmd):
        return {"status": "skipped", "message": f"{base_cmd} not available"}

    resolved = str(Path(filepath).expanduser().resolve())
    cmd = linter_template.format(file=repr(resolved))
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return {"status": "ok"}
        output = (result.stdout or "") + (result.stderr or "")
        return {"status": "error", "output": output.strip()}
    except subprocess.TimeoutExpired:
        return {"status": "skipped", "message": "lint timed out"}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Suggest similar files — when read_file can't find a file
# ---------------------------------------------------------------------------


def _suggest_similar_files(path: str) -> list[str]:
    """Return up to 5 similar filenames in the same directory.

    Uses difflib.get_close_matches for fuzzy filename matching.
    """
    dir_path = os.path.dirname(path) or "."
    filename = os.path.basename(path)

    try:
        resolved_dir = str(Path(dir_path).expanduser().resolve())
        entries = os.listdir(resolved_dir)
    except OSError:
        return []

    matches = difflib.get_close_matches(filename, entries, n=5, cutoff=0.4)
    return [os.path.join(dir_path, m) for m in matches]


# ---------------------------------------------------------------------------
# Read-size guard: cap the character count returned to the model.
# We're model-agnostic so we can't count tokens; characters are a safe proxy.
# 100K chars ≈ 25–35K tokens across typical tokenisers.  Files larger than
# this in a single read are a context-window hazard — the model should use
# offset+limit to read the relevant section.
#
# Configurable via config.yaml:  file_read_max_chars: 200000
# ---------------------------------------------------------------------------
_DEFAULT_MAX_READ_CHARS = 100_000

# If the total file size exceeds this AND the caller didn't specify a narrow
# range (limit <= 200), we include a hint encouraging targeted reads.
_LARGE_FILE_HINT_BYTES = 512_000  # 512 KB

# ---------------------------------------------------------------------------
# Binary file extensions — imported from shared utils module.
# ---------------------------------------------------------------------------
from surogates.tools.utils.binary_extensions import BINARY_EXTENSIONS, has_binary_extension


# ---------------------------------------------------------------------------
# Device path blocklist — reading these hangs the process (infinite output
# or blocking on input).  Checked by path only (no I/O).
# ---------------------------------------------------------------------------
_BLOCKED_DEVICE_PATHS = frozenset({
    # Infinite output — never reach EOF
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    # Blocks waiting for input
    "/dev/stdin", "/dev/tty", "/dev/console",
    # Nonsensical to read
    "/dev/stdout", "/dev/stderr",
    # fd aliases
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})


def _is_blocked_device(filepath: str) -> bool:
    """Return True if the path would hang the process (infinite output or blocking input).

    Uses the *literal* path — no symlink resolution — because the model
    specifies paths directly and realpath follows symlinks all the way
    through (e.g. /dev/stdin → /proc/self/fd/0 → /dev/pts/0), defeating
    the check.
    """
    normalized = os.path.expanduser(filepath)
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    # /proc/self/fd/0-2 and /proc/<pid>/fd/0-2 are Linux aliases for stdio
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/fd/0", "/fd/1", "/fd/2")
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Sensitive path protection — refuse writes to system-critical locations
# without going through the terminal tool's approval system.
# ---------------------------------------------------------------------------
_SENSITIVE_PATH_PREFIXES = ("/etc/", "/boot/", "/usr/lib/systemd/")
_SENSITIVE_EXACT_PATHS = {"/var/run/docker.sock", "/run/docker.sock"}


def _check_sensitive_path(filepath: str) -> str | None:
    """Return an error message if the path targets a sensitive system location."""
    try:
        resolved = os.path.realpath(os.path.expanduser(filepath))
    except (OSError, ValueError):
        resolved = filepath
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if resolved.startswith(prefix):
            return (
                f"Refusing to write to sensitive system path: {filepath}\n"
                "Use the terminal tool with sudo if you need to modify system files."
            )
    if resolved in _SENSITIVE_EXACT_PATHS:
        return (
            f"Refusing to write to sensitive system path: {filepath}\n"
            "Use the terminal tool with sudo if you need to modify system files."
        )
    return None


def _is_expected_write_exception(exc: Exception) -> bool:
    """Return True for expected write denials that should not hit error logs."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno in _EXPECTED_WRITE_ERRNOS:
        return True
    return False


# ---------------------------------------------------------------------------
# Read tracker — detect re-read loops and deduplicate reads.
# Per task_id we store:
#   "last_key":     the key of the most recent read/search call (or None)
#   "consecutive":  how many times that exact call has been repeated in a row
#   "read_history": set of (path, offset, limit) tuples for get_read_files_summary
#   "dedup":        dict mapping (resolved_path, offset, limit) → mtime float
#                   Used to skip re-reads of unchanged files.  Reset on
#                   context compression (the original content is summarised
#                   away so the model needs the full content again).
#   "read_timestamps": dict mapping resolved_path → modification-time float
#                      recorded when the file was last read (or written) by
#                      this task.  Used by write_file and patch to detect
#                      external changes between the agent's read and write.
#                      Updated after successful writes so consecutive edits
#                      by the same task don't trigger false warnings.
# ---------------------------------------------------------------------------
_read_tracker_lock = threading.Lock()
_read_tracker: dict = {}


def _init_task_data(task_id: str) -> dict:
    """Return (and lazily create) the tracker state dict for *task_id*."""
    with _read_tracker_lock:
        task_data = _read_tracker.setdefault(task_id, {
            "last_key": None,
            "consecutive": 0,
            "read_history": set(),
            "dedup": {},
            "read_timestamps": {},
        })
        # Backward compat: ensure all keys exist for older tracker state
        task_data.setdefault("dedup", {})
        task_data.setdefault("read_timestamps", {})
        return task_data


def get_read_files_summary(task_id: str = "default") -> list:
    """Return a list of files read in this session for the given task.

    Used by context compression to preserve file-read history across
    compression boundaries.
    """
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id, {})
        read_history = task_data.get("read_history", set())
        seen_paths: dict = {}
        for (path, offset, limit) in read_history:
            if path not in seen_paths:
                seen_paths[path] = []
            seen_paths[path].append(f"lines {offset}-{offset + limit - 1}")
        return [
            {"path": p, "regions": regions}
            for p, regions in sorted(seen_paths.items())
        ]


def clear_read_tracker(task_id: str | None = None) -> None:
    """Clear the read tracker.

    Call with a task_id to clear just that task, or without to clear all.
    Should be called when a session is destroyed to prevent memory leaks
    in long-running gateway processes.
    """
    with _read_tracker_lock:
        if task_id:
            _read_tracker.pop(task_id, None)
        else:
            _read_tracker.clear()


def reset_file_dedup(task_id: str | None = None) -> None:
    """Clear the deduplication cache for file reads.

    Called after context compression — the original read content has been
    summarised away, so the model needs the full content if it reads the
    same file again.  Without this, reads after compression would return
    a "file unchanged" stub pointing at content that no longer exists in
    context.

    Call with a task_id to clear just that task, or without to clear all.
    """
    with _read_tracker_lock:
        if task_id:
            task_data = _read_tracker.get(task_id)
            if task_data and "dedup" in task_data:
                task_data["dedup"].clear()
        else:
            for task_data in _read_tracker.values():
                if "dedup" in task_data:
                    task_data["dedup"].clear()


def notify_other_tool_call(task_id: str = "default") -> None:
    """Reset consecutive read/search counter for a task.

    Called by the tool dispatcher whenever a tool OTHER than read_file /
    search_files is executed.  This ensures we only warn or block on
    *truly consecutive* repeated reads — if the agent does anything else
    in between (write, patch, terminal, etc.) the counter resets and the
    next read is treated as fresh.
    """
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data:
            task_data["last_key"] = None
            task_data["consecutive"] = 0


def _update_read_timestamp(filepath: str, task_id: str) -> None:
    """Record the file's current modification time after a successful write.

    Called after write_file and patch so that consecutive edits by the
    same task don't trigger false staleness warnings — each write
    refreshes the stored timestamp to match the file's new state.
    """
    try:
        resolved = str(Path(filepath).expanduser().resolve())
        current_mtime = os.path.getmtime(resolved)
    except (OSError, ValueError):
        return
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data is not None:
            task_data.setdefault("read_timestamps", {})[resolved] = current_mtime


def _check_file_staleness(filepath: str, task_id: str) -> str | None:
    """Check whether a file was modified since the agent last read it.

    Returns a warning string if the file is stale (mtime changed since
    the last read_file call for this task), or None if the file is fresh
    or was never read.  Does not block — the write still proceeds.
    """
    try:
        resolved = str(Path(filepath).expanduser().resolve())
    except (OSError, ValueError):
        return None
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if not task_data:
            return None
        read_mtime = task_data.get("read_timestamps", {}).get(resolved)
    if read_mtime is None:
        return None  # File was never read — nothing to compare against
    try:
        current_mtime = os.path.getmtime(resolved)
    except OSError:
        return None  # Can't stat — file may have been deleted, let write handle it
    if current_mtime != read_mtime:
        return (
            f"Warning: {filepath} was modified since you last read it "
            "(external edit or concurrent agent). The content you read may be "
            "stale. Consider re-reading the file to verify before writing."
        )
    return None


def _tool_error(message: str) -> str:
    """Return a JSON error response string."""
    return json.dumps({"error": message})


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

READ_FILE_SCHEMA = ToolSchema(
    name="read_file",
    description=(
        "Read a text file with line numbers and pagination. Use this instead of "
        "cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Suggests "
        "similar filenames if not found. Use offset and limit for large files. "
        "Reads exceeding ~100K characters are rejected; use offset and limit to "
        "read specific sections of large files. NOTE: Cannot read images or binary "
        "files — use vision_analyze for images."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read (absolute, relative, or ~/path)",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed, default: 1)",
                "default": 1,
                "minimum": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read (default: 500, max: 2000)",
                "default": 500,
                "maximum": 2000,
            },
        },
        "required": ["path"],
    },
)

WRITE_FILE_SCHEMA = ToolSchema(
    name="write_file",
    description=(
        "Write content to a file, completely replacing existing content. Use this "
        "instead of echo/cat heredoc in terminal. Creates parent directories "
        "automatically. OVERWRITES the entire file — use 'patch' for targeted edits."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the file to write (will be created if it doesn't exist, "
                    "overwritten if it does)"
                ),
            },
            "content": {
                "type": "string",
                "description": "Complete content to write to the file",
            },
        },
        "required": ["path", "content"],
    },
)

PATCH_SCHEMA = ToolSchema(
    name="patch",
    description=(
        "Targeted find-and-replace edits in files. Use this instead of sed/awk in "
        "terminal. Uses fuzzy matching (9 strategies) so minor whitespace/indentation "
        "differences won't break it. Returns a unified diff. Auto-runs syntax checks "
        "after editing.\n\n"
        "Replace mode (default): find a unique string and replace it.\n"
        "Patch mode: apply V4A multi-file patches for bulk changes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["replace", "patch"],
                "description": (
                    "Edit mode: 'replace' for targeted find-and-replace, "
                    "'patch' for V4A multi-file patches"
                ),
                "default": "replace",
            },
            "path": {
                "type": "string",
                "description": "File path to edit (required for 'replace' mode)",
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Text to find in the file (required for 'replace' mode). "
                    "Must be unique in the file unless replace_all=true. Include "
                    "enough surrounding context to ensure uniqueness."
                ),
            },
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement text (required for 'replace' mode). Can be "
                    "empty string to delete the matched text."
                ),
            },
            "replace_all": {
                "type": "boolean",
                "description": (
                    "Replace all occurrences instead of requiring a unique match "
                    "(default: false)"
                ),
                "default": False,
            },
            "patch": {
                "type": "string",
                "description": (
                    "V4A format patch content (required for 'patch' mode). Format:\n"
                    "*** Begin Patch\n"
                    "*** Update File: path/to/file\n"
                    "@@ context hint @@\n"
                    " context line\n"
                    "-removed line\n"
                    "+added line\n"
                    "*** End Patch"
                ),
            },
        },
        "required": ["mode"],
    },
)

SEARCH_FILES_SCHEMA = ToolSchema(
    name="search_files",
    description=(
        "Search file contents or find files by name. Use this instead of "
        "grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents.\n\n"
        "Content search (target='content'): Regex search inside files. Output modes: "
        "full matches with line numbers, file paths only, or match counts.\n\n"
        "File search (target='files'): Find files by glob pattern (e.g., '*.py', "
        "'*config*'). Also use this instead of ls — results sorted by modification time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Regex pattern for content search, or glob pattern "
                    "(e.g., '*.py') for file search"
                ),
            },
            "target": {
                "type": "string",
                "enum": ["content", "files"],
                "description": (
                    "'content' searches inside file contents, "
                    "'files' searches for files by name"
                ),
                "default": "content",
            },
            "path": {
                "type": "string",
                "description": (
                    "Directory or file to search in "
                    "(default: current working directory)"
                ),
                "default": ".",
            },
            "file_glob": {
                "type": "string",
                "description": (
                    "Filter files by pattern in grep mode "
                    "(e.g., '*.py' to only search Python files)"
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 50)",
                "default": 50,
            },
            "offset": {
                "type": "integer",
                "description": "Skip first N results for pagination (default: 0)",
                "default": 0,
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_only", "count"],
                "description": (
                    "Output format for grep mode: 'content' shows matching lines "
                    "with line numbers, 'files_only' lists file paths, 'count' "
                    "shows match counts per file"
                ),
                "default": "content",
            },
            "context": {
                "type": "integer",
                "description": (
                    "Number of context lines before and after each match "
                    "(grep mode only)"
                ),
                "default": 0,
            },
        },
        "required": ["pattern"],
    },
)

LIST_FILES_SCHEMA = ToolSchema(
    name="list_files",
    description=(
        "List directory contents. Equivalent to search_files with "
        "target='files'. Returns files sorted by modification time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory to list (default: current working directory)",
                "default": ".",
            },
            "pattern": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g., '*.py')",
                "default": "*",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 50)",
                "default": 50,
            },
        },
        "required": [],
    },
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    """Register read_file, write_file, patch, search_files, and list_files tools."""

    # ── read_file ─────────────────────────────────────────────────────
    registry.register(
        name="read_file",
        schema=READ_FILE_SCHEMA,
        handler=_read_file_handler,
        toolset="file",
        max_result_size=100_000,
    )

    # ── write_file ────────────────────────────────────────────────────
    registry.register(
        name="write_file",
        schema=WRITE_FILE_SCHEMA,
        handler=_write_file_handler,
        toolset="file",
        max_result_size=100_000,
    )

    # ── patch ─────────────────────────────────────────────────────────
    registry.register(
        name="patch",
        schema=PATCH_SCHEMA,
        handler=_patch_handler,
        toolset="file",
        max_result_size=100_000,
    )

    # ── search_files ──────────────────────────────────────────────────
    registry.register(
        name="search_files",
        schema=SEARCH_FILES_SCHEMA,
        handler=_search_files_handler,
        toolset="file",
        max_result_size=100_000,
    )

    # ── list_files ────────────────────────────────────────────────────
    registry.register(
        name="list_files",
        schema=LIST_FILES_SCHEMA,
        handler=_list_files_handler,
        toolset="file",
        max_result_size=100_000,
    )


# ---------------------------------------------------------------------------
# Harness-local fallback handlers
# ---------------------------------------------------------------------------


async def _read_file_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Harness-local fallback: read a file with line numbers and pagination.

    In production, the ToolRouter sends read_file to the sandbox runtime
    instead of invoking this handler.  Implements the read_file
    logic: device path guard, binary file guard, character-count guard,
    large-file hint, dedup check, and consecutive-loop detection.
    """
    path = arguments.get("path", "")
    offset = max(arguments.get("offset", 1), 1)
    limit = min(arguments.get("limit", 500), 2000)
    task_id = kwargs.get("task_id", "default")

    if not path:
        return _tool_error("No path provided")

    try:
        # ── Device path guard ─────────────────────────────────────────
        # Block paths that would hang the process (infinite output,
        # blocking on input).  Pure path check — no I/O.
        if _is_blocked_device(path):
            return json.dumps({
                "error": (
                    f"Cannot read '{path}': this is a device file that would "
                    "block or produce infinite output."
                ),
            })

        _resolved = Path(path).expanduser().resolve()

        # ── Image file guard ─────────────────────────────────────────
        # Images are never inlined — redirect to the vision tool.
        if _is_image(str(_resolved)):
            return json.dumps({
                "error": (
                    f"Image file detected: '{path}'. "
                    "Use vision_analyze with this file path to inspect the image contents."
                ),
            })

        # ── Binary file guard ─────────────────────────────────────────
        # Block binary files by extension (no I/O).
        if has_binary_extension(str(_resolved)):
            _ext = _resolved.suffix.lower()
            return json.dumps({
                "error": (
                    f"Cannot read binary file '{path}' ({_ext}). "
                    "Use vision_analyze for images, or terminal to inspect binary files."
                ),
            })

        resolved_str = str(_resolved)

        # ── Dedup check ───────────────────────────────────────────────
        # If we already read this exact (path, offset, limit) and the
        # file hasn't been modified since, return a lightweight stub
        # instead of re-sending the same content.  Saves context tokens.
        dedup_key = (resolved_str, offset, limit)
        task_data = _init_task_data(task_id)
        with _read_tracker_lock:
            cached_mtime = task_data.get("dedup", {}).get(dedup_key)

        if cached_mtime is not None:
            try:
                current_mtime = os.path.getmtime(resolved_str)
                if current_mtime == cached_mtime:
                    return json.dumps({
                        "content": (
                            "File unchanged since last read. The content from "
                            "the earlier read_file result in this conversation is "
                            "still current — refer to that instead of re-reading."
                        ),
                        "path": path,
                        "dedup": True,
                    }, ensure_ascii=False)
            except OSError:
                pass  # stat failed — fall through to full read

        # ── Perform the read ──────────────────────────────────────────
        if not os.path.exists(resolved_str):
            result_dict: dict[str, Any] = {"error": f"File not found: {path}"}
            similar = _suggest_similar_files(path)
            if similar:
                result_dict["similar_files"] = similar
                result_dict["hint"] = (
                    "Did you mean one of these files? "
                    + ", ".join(similar)
                )
            return json.dumps(result_dict, ensure_ascii=False)

        file_size = os.path.getsize(resolved_str)

        # Try to detect encoding; fall back to utf-8
        encoding = "utf-8"
        try:
            with open(resolved_str, "rb") as fb:
                raw_head = fb.read(8192)
            # Check for UTF-16/UTF-32 BOM
            if raw_head.startswith(b"\xff\xfe\x00\x00"):
                encoding = "utf-32-le"
            elif raw_head.startswith(b"\x00\x00\xfe\xff"):
                encoding = "utf-32-be"
            elif raw_head.startswith(b"\xff\xfe"):
                encoding = "utf-16-le"
            elif raw_head.startswith(b"\xfe\xff"):
                encoding = "utf-16-be"
            elif raw_head.startswith(b"\xef\xbb\xbf"):
                encoding = "utf-8-sig"
            else:
                # Check for NUL bytes indicating binary content
                if b"\x00" in raw_head:
                    return json.dumps({
                        "error": (
                            f"Cannot read binary file '{path}'. "
                            "Use vision_analyze for images, or terminal to inspect binary files."
                        ),
                    })
        except OSError:
            pass  # Proceed with utf-8

        try:
            with open(resolved_str, encoding=encoding, errors="replace") as fh:
                lines = fh.readlines()
        except (OSError, UnicodeDecodeError) as exc:
            return _tool_error(f"Failed to read file: {exc}")

        total_lines = len(lines)
        start_idx = offset - 1  # Convert 1-indexed to 0-indexed
        end_idx = min(start_idx + limit, total_lines)
        selected = lines[start_idx:end_idx]

        # Format with line numbers
        content = ""
        for i, line in enumerate(selected, start=offset):
            content += f"{i}|{line}"

        # ── Character-count guard ─────────────────────────────────────
        # We're model-agnostic so we can't count tokens; characters are
        # the best proxy we have.  If the read produced an unreasonable
        # amount of content, reject it and tell the model to narrow down.
        # Note: we check the formatted content (with line-number prefixes),
        # not the raw file size, because that's what actually enters context.
        content_len = len(content)
        max_chars = _DEFAULT_MAX_READ_CHARS
        if content_len > max_chars:
            return json.dumps({
                "error": (
                    f"Read produced {content_len:,} characters which exceeds "
                    f"the safety limit ({max_chars:,} chars). "
                    "Use offset and limit to read a smaller range. "
                    f"The file has {total_lines} lines total."
                ),
                "path": path,
                "total_lines": total_lines,
                "file_size": file_size,
            }, ensure_ascii=False)

        truncated = end_idx < total_lines

        result_dict: dict[str, Any] = {
            "content": content,
            "path": path,
            "total_lines": total_lines,
            "lines_shown": len(selected),
            "offset": offset,
            "limit": limit,
            "truncated": truncated,
            "file_size": file_size,
        }

        # Large-file hint: if the file is big and the caller didn't ask
        # for a narrow window, nudge toward targeted reads.
        if (file_size and file_size > _LARGE_FILE_HINT_BYTES
                and limit > 200
                and truncated):
            result_dict["_hint"] = (
                f"This file is large ({file_size:,} bytes). "
                "Consider reading only the section you need with offset and limit "
                "to keep context usage efficient."
            )

        # ── Track for consecutive-loop detection ──────────────────────
        read_key = ("read", path, offset, limit)
        with _read_tracker_lock:
            task_data["read_history"].add((path, offset, limit))
            if task_data["last_key"] == read_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = read_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

            # Store mtime at read time for two purposes:
            # 1. Dedup: skip identical re-reads of unchanged files.
            # 2. Staleness: warn on write/patch if the file changed since
            #    the agent last read it (external edit, concurrent agent, etc.).
            try:
                _mtime_now = os.path.getmtime(resolved_str)
                task_data["dedup"][dedup_key] = _mtime_now
                task_data.setdefault("read_timestamps", {})[resolved_str] = _mtime_now
            except OSError:
                pass  # Can't stat — skip tracking for this entry

        if count >= 4:
            # Hard block: stop returning content to break the loop
            return json.dumps({
                "error": (
                    f"BLOCKED: You have read this exact file region {count} times in a row. "
                    "The content has NOT changed. You already have this information. "
                    "STOP re-reading and proceed with your task."
                ),
                "path": path,
                "already_read": count,
            }, ensure_ascii=False)
        elif count >= 3:
            result_dict["_warning"] = (
                f"You have read this exact file region {count} times consecutively. "
                "The content has not changed since your last read. Use the information you already have. "
                "If you are stuck in a loop, stop reading and proceed with writing or responding."
            )

        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as exc:
        return _tool_error(str(exc))


async def _write_file_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Harness-local fallback: write content to a file.

    In production, the ToolRouter sends write_file to the sandbox runtime
    instead of invoking this handler.  Implements the write_file
    logic: sensitive path check, staleness warning, atomic write, directory
    creation, and read-timestamp refresh.
    """
    path = arguments.get("path", "")
    content = arguments.get("content", "")
    task_id = kwargs.get("task_id", "default")

    if not path:
        return _tool_error("No path provided")

    # Block writes to sensitive system/credential files
    if _is_write_denied(path):
        return _tool_error(
            f"Write denied: '{path}' is a protected system/credential file."
        )

    sensitive_err = _check_sensitive_path(path)
    if sensitive_err:
        return _tool_error(sensitive_err)

    try:
        stale_warning = _check_file_staleness(path, task_id)

        expanded = os.path.expanduser(path)
        resolved = str(Path(expanded).resolve())

        # Create parent directories automatically
        parent_dir = os.path.dirname(resolved) or "."
        os.makedirs(parent_dir, exist_ok=True)

        # Atomic write: write to temp file then rename
        tmp_path = resolved + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp_path, resolved)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        lines_written = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        result_dict: dict[str, Any] = {
            "status": "ok",
            "path": path,
            "bytes_written": len(content.encode("utf-8")),
            "lines_written": lines_written,
        }

        if stale_warning:
            result_dict["_warning"] = stale_warning

        # Auto-lint after write
        lint_result = _check_lint(path)
        if lint_result:
            result_dict["lint"] = lint_result

        # Refresh the stored timestamp so consecutive writes by this
        # task don't trigger false staleness warnings.
        _update_read_timestamp(path, task_id)

        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as exc:
        if _is_expected_write_exception(exc):
            logger.debug("write_file expected denial: %s: %s", type(exc).__name__, exc)
        else:
            logger.error("write_file error: %s: %s", type(exc).__name__, exc, exc_info=True)
        return _tool_error(str(exc))


async def _patch_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Harness-local fallback: find-and-replace in a file.

    In production, the ToolRouter sends patch to the sandbox runtime
    instead of invoking this handler.  Implements the patch
    logic: sensitive path check, staleness warning, replace mode with
    fuzzy matching, V4A patch mode, and read-timestamp refresh.
    """
    mode = arguments.get("mode", "replace")
    path = arguments.get("path", "")
    old_string = arguments.get("old_string")
    new_string = arguments.get("new_string")
    replace_all = arguments.get("replace_all", False)
    patch_content = arguments.get("patch")
    task_id = kwargs.get("task_id", "default")

    # Check sensitive paths for both replace (explicit path) and V4A patch (extract paths)
    paths_to_check: list[str] = []
    if path:
        paths_to_check.append(path)
    if mode == "patch" and patch_content:
        for m in re.finditer(
            r'^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)$',
            patch_content,
            re.MULTILINE,
        ):
            paths_to_check.append(m.group(1).strip())

    for p in paths_to_check:
        # Block writes to sensitive system/credential files
        if _is_write_denied(p):
            return _tool_error(
                f"Write denied: '{p}' is a protected system/credential file."
            )
        sensitive_err = _check_sensitive_path(p)
        if sensitive_err:
            return _tool_error(sensitive_err)

    try:
        # Check staleness for all files this patch will touch.
        stale_warnings: list[str] = []
        for p in paths_to_check:
            sw = _check_file_staleness(p, task_id)
            if sw:
                stale_warnings.append(sw)

        if mode == "patch":
            if not patch_content:
                return _tool_error("patch content required")
            # V4A patch mode — apply multi-file patches
            result_dict = _apply_v4a_patch(patch_content)
        elif mode == "replace":
            if not path:
                return _tool_error("path required")
            if old_string is None or new_string is None:
                return _tool_error("old_string and new_string required")
            result_dict = _apply_replace(path, old_string, new_string, replace_all)
        else:
            return _tool_error(f"Unknown mode: {mode}")

        if stale_warnings:
            result_dict["_warning"] = (
                stale_warnings[0]
                if len(stale_warnings) == 1
                else " | ".join(stale_warnings)
            )

        # Auto-lint after successful patch
        if not result_dict.get("error"):
            for p in paths_to_check:
                lint_result = _check_lint(p)
                if lint_result and lint_result.get("status") != "skipped":
                    result_dict.setdefault("lint", {})[p] = lint_result

        # Refresh stored timestamps for all successfully-patched paths so
        # consecutive edits by this task don't trigger false warnings.
        if not result_dict.get("error"):
            for p in paths_to_check:
                _update_read_timestamp(p, task_id)

        result_json = json.dumps(result_dict, ensure_ascii=False)

        # Hint when old_string not found — saves iterations where the agent
        # retries with stale content instead of re-reading the file.
        if result_dict.get("error") and "Could not find" in str(result_dict["error"]):
            result_json += (
                "\n\n[Hint: old_string not found. Use read_file to verify the "
                "current content, or search_files to locate the text.]"
            )

        return result_json
    except Exception as exc:
        return _tool_error(str(exc))


def _apply_replace(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
) -> dict[str, Any]:
    """Apply a find-and-replace edit to a single file.

    Tries exact match first, then falls back through multiple fuzzy
    matching strategies (9 total) to handle whitespace/indentation
    differences.  Returns a result dict.
    """
    expanded = os.path.expanduser(path)
    resolved = str(Path(expanded).resolve())

    if not os.path.exists(resolved):
        return {"error": f"File not found: {path}"}

    try:
        with open(resolved, encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        return {"error": f"Failed to read file: {exc}"}

    # --- Exact match ---
    if old_string in content:
        if not replace_all:
            count = content.count(old_string)
            if count > 1:
                return {
                    "error": (
                        f"Found {count} occurrences of old_string in {path}. "
                        "Include more surrounding context to make it unique, "
                        "or set replace_all=true."
                    ),
                    "path": path,
                    "occurrences": count,
                }
            new_content = content.replace(old_string, new_string, 1)
        else:
            new_content = content.replace(old_string, new_string)

        return _write_patched(resolved, path, content, new_content)

    # --- Fuzzy matching strategies ---
    # Strategy 1: Normalize line endings (CRLF → LF)
    normalized_content = content.replace("\r\n", "\n")
    normalized_old = old_string.replace("\r\n", "\n")
    if normalized_old in normalized_content:
        if not replace_all:
            new_content = normalized_content.replace(normalized_old, new_string, 1)
        else:
            new_content = normalized_content.replace(normalized_old, new_string)
        return _write_patched(resolved, path, content, new_content)

    # Strategy 2: Strip trailing whitespace from each line
    def strip_trailing(text: str) -> str:
        return "\n".join(line.rstrip() for line in text.split("\n"))

    stripped_content = strip_trailing(content)
    stripped_old = strip_trailing(old_string)
    if stripped_old in stripped_content:
        if not replace_all:
            new_content = stripped_content.replace(stripped_old, new_string, 1)
        else:
            new_content = stripped_content.replace(stripped_old, new_string)
        return _write_patched(resolved, path, content, new_content)

    # Strategy 3: Tabs ↔ spaces (try both 2-space and 4-space)
    for tab_width in (4, 2):
        spaces = " " * tab_width
        content_tabs_as_spaces = content.replace("\t", spaces)
        old_tabs_as_spaces = old_string.replace("\t", spaces)
        if old_tabs_as_spaces in content_tabs_as_spaces:
            if not replace_all:
                new_content = content_tabs_as_spaces.replace(
                    old_tabs_as_spaces, new_string.replace("\t", spaces), 1
                )
            else:
                new_content = content_tabs_as_spaces.replace(
                    old_tabs_as_spaces, new_string.replace("\t", spaces)
                )
            return _write_patched(resolved, path, content, new_content)

        # Try the reverse: spaces → tabs
        content_spaces_as_tabs = content.replace(spaces, "\t")
        old_spaces_as_tabs = old_string.replace(spaces, "\t")
        if old_spaces_as_tabs in content_spaces_as_tabs:
            if not replace_all:
                new_content = content_spaces_as_tabs.replace(
                    old_spaces_as_tabs, new_string.replace(spaces, "\t"), 1
                )
            else:
                new_content = content_spaces_as_tabs.replace(
                    old_spaces_as_tabs, new_string.replace(spaces, "\t")
                )
            return _write_patched(resolved, path, content, new_content)

    # Strategy 4: Collapse all whitespace runs to single space
    def collapse_ws(text: str) -> str:
        return re.sub(r"[ \t]+", " ", text)

    collapsed_content = collapse_ws(content)
    collapsed_old = collapse_ws(old_string)
    if collapsed_old in collapsed_content:
        # Find the position in collapsed, map back to original
        # For simplicity, just do the replace on original with best-effort
        # line-by-line matching
        if not replace_all:
            new_content = collapsed_content.replace(collapsed_old, collapse_ws(new_string), 1)
        else:
            new_content = collapsed_content.replace(collapsed_old, collapse_ws(new_string))
        return _write_patched(resolved, path, content, new_content)

    # Strategy 5: Indentation-agnostic matching
    # Dedent both old_string and content lines, try to match
    old_lines = old_string.split("\n")
    if len(old_lines) >= 2:
        import textwrap

        dedented_old = textwrap.dedent(old_string)
        dedented_content = textwrap.dedent(content)
        if dedented_old in dedented_content:
            if not replace_all:
                new_content = dedented_content.replace(
                    dedented_old, textwrap.dedent(new_string), 1
                )
            else:
                new_content = dedented_content.replace(
                    dedented_old, textwrap.dedent(new_string)
                )
            return _write_patched(resolved, path, content, new_content)

    # Strategy 6: Case-insensitive matching (last resort for minor typos)
    lower_content = content.lower()
    lower_old = old_string.lower()
    if lower_old in lower_content:
        # Find the actual case in content and replace it
        idx = lower_content.find(lower_old)
        actual = content[idx : idx + len(old_string)]
        if not replace_all:
            new_content = content.replace(actual, new_string, 1)
        else:
            new_content = content.replace(actual, new_string)
        return _write_patched(resolved, path, content, new_content)

    return {
        "error": f"Could not find the specified text in {path}",
        "path": path,
    }


def _apply_v4a_patch(patch_content: str) -> dict[str, Any]:
    """Apply a V4A-format multi-file patch.

    Parses the V4A patch format and applies each file operation
    (Update, Add, Delete) in sequence.

    V4A format::

        *** Begin Patch
        *** Update File: path/to/file
        @@ context hint @@
         context line
        -removed line
        +added line
        *** End Patch
    """
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    # Parse patch into file operations
    lines = patch_content.split("\n")
    current_file: str | None = None
    current_op: str | None = None  # "Update", "Add", "Delete"
    current_hunks: list[str] = []

    for line in lines:
        # Skip begin/end markers
        if line.strip() in ("*** Begin Patch", "*** End Patch"):
            if current_file and current_hunks:
                result = _apply_v4a_file_op(current_file, current_op or "Update", current_hunks)
                results.append(result)
                if result.get("error"):
                    errors.append(result["error"])
            current_file = None
            current_op = None
            current_hunks = []
            continue

        # File operation header
        m = re.match(r'^\*\*\*\s+(Update|Add|Delete)\s+File:\s*(.+)$', line)
        if m:
            # Flush previous file
            if current_file and current_hunks:
                result = _apply_v4a_file_op(current_file, current_op or "Update", current_hunks)
                results.append(result)
                if result.get("error"):
                    errors.append(result["error"])
            current_op = m.group(1)
            current_file = m.group(2).strip()
            current_hunks = []
            continue

        # Accumulate hunk lines
        if current_file is not None:
            current_hunks.append(line)

    # Flush final file
    if current_file and current_hunks:
        result = _apply_v4a_file_op(current_file, current_op or "Update", current_hunks)
        results.append(result)
        if result.get("error"):
            errors.append(result["error"])

    if errors:
        return {
            "status": "partial" if results else "error",
            "files": results,
            "errors": errors,
        }
    return {
        "status": "ok",
        "files": results,
    }


def _apply_v4a_file_op(
    filepath: str,
    operation: str,
    hunk_lines: list[str],
) -> dict[str, Any]:
    """Apply a single V4A file operation (Update, Add, or Delete)."""
    expanded = os.path.expanduser(filepath)
    resolved = str(Path(expanded).resolve())

    if operation == "Delete":
        try:
            os.unlink(resolved)
            return {"path": filepath, "operation": "deleted", "status": "ok"}
        except OSError as exc:
            return {"path": filepath, "error": f"Failed to delete: {exc}"}

    if operation == "Add":
        # Collect all '+' lines as new content
        content_lines = []
        for line in hunk_lines:
            if line.startswith("+"):
                content_lines.append(line[1:])
            elif line.startswith(" "):
                content_lines.append(line[1:])
        content = "\n".join(content_lines)
        try:
            parent = os.path.dirname(resolved) or "."
            os.makedirs(parent, exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as fh:
                fh.write(content)
            return {
                "path": filepath,
                "operation": "created",
                "status": "ok",
                "bytes_written": len(content.encode("utf-8")),
            }
        except OSError as exc:
            return {"path": filepath, "error": f"Failed to create: {exc}"}

    # Operation == "Update"
    if not os.path.exists(resolved):
        return {"path": filepath, "error": f"File not found: {filepath}"}

    try:
        with open(resolved, encoding="utf-8") as fh:
            original = fh.read()
    except OSError as exc:
        return {"path": filepath, "error": f"Failed to read: {exc}"}

    original_lines = original.split("\n")
    new_lines = list(original_lines)
    current_idx = 0

    # Process hunks
    i = 0
    while i < len(hunk_lines):
        line = hunk_lines[i]

        # Skip context hints
        if line.startswith("@@"):
            # Try to use the context hint to find position
            hint = line.strip("@ \n")
            if hint:
                for j, orig_line in enumerate(new_lines[current_idx:], current_idx):
                    if hint in orig_line:
                        current_idx = j
                        break
            i += 1
            continue

        if line.startswith(" "):
            # Context line — advance position
            context_text = line[1:]
            # Find this context line starting from current position
            found = False
            for j in range(current_idx, len(new_lines)):
                if new_lines[j].rstrip() == context_text.rstrip():
                    current_idx = j + 1
                    found = True
                    break
            if not found:
                # Try fuzzy: strip whitespace
                for j in range(current_idx, len(new_lines)):
                    if new_lines[j].strip() == context_text.strip():
                        current_idx = j + 1
                        found = True
                        break
            i += 1
            continue

        if line.startswith("-"):
            # Remove line
            remove_text = line[1:]
            found = False
            for j in range(max(0, current_idx - 1), len(new_lines)):
                if new_lines[j].rstrip() == remove_text.rstrip():
                    new_lines.pop(j)
                    current_idx = j
                    found = True
                    break
            if not found:
                # Fuzzy removal
                for j in range(max(0, current_idx - 1), len(new_lines)):
                    if new_lines[j].strip() == remove_text.strip():
                        new_lines.pop(j)
                        current_idx = j
                        found = True
                        break
            i += 1
            continue

        if line.startswith("+"):
            # Add line
            add_text = line[1:]
            new_lines.insert(current_idx, add_text)
            current_idx += 1
            i += 1
            continue

        # Unknown line — skip
        i += 1

    new_content = "\n".join(new_lines)
    try:
        with open(resolved, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        return {
            "path": filepath,
            "operation": "updated",
            "status": "ok",
            "bytes_written": len(new_content.encode("utf-8")),
        }
    except OSError as exc:
        return {"path": filepath, "error": f"Failed to write: {exc}"}


def _write_patched(
    resolved_path: str,
    display_path: str,
    original: str,
    new_content: str,
) -> dict[str, Any]:
    """Write patched content and return a unified diff result.

    Used by ``_apply_replace`` after a successful match (exact or fuzzy).
    """
    import difflib

    try:
        # Atomic write
        tmp_path = resolved_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            os.replace(tmp_path, resolved_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Generate unified diff for the response
        original_lines = original.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            original_lines,
            new_lines,
            fromfile=f"a/{display_path}",
            tofile=f"b/{display_path}",
        )
        diff_text = "".join(diff)

        return {
            "status": "ok",
            "path": display_path,
            "diff": diff_text if diff_text else "(no visible diff — whitespace-only change)",
        }
    except OSError as exc:
        return {"error": f"Failed to write patched file: {exc}"}


async def _search_files_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Harness-local fallback: search file contents or find files by name.

    In production, the ToolRouter sends search_files to the sandbox
    runtime instead of invoking this handler.  Implements the search_files
    logic: content search with regex, file search with glob,
    output modes, pagination, context lines, and consecutive-loop detection.
    """
    import glob as glob_mod

    pattern = arguments.get("pattern", "")
    target = arguments.get("target", "content")
    path = arguments.get("path", ".")
    file_glob = arguments.get("file_glob")
    limit = arguments.get("limit", 50)
    offset = arguments.get("offset", 0)
    output_mode = arguments.get("output_mode", "content")
    context = arguments.get("context", 0)
    task_id = kwargs.get("task_id", "default")

    # Map legacy target names
    target_map = {"grep": "content", "find": "files"}
    target = target_map.get(target, target)

    if not pattern:
        return _tool_error("No pattern provided")

    try:
        # Track searches to detect *consecutive* repeated search loops.
        # Include pagination args so users can page through truncated
        # results without tripping the repeated-search guard.
        search_key = (
            "search",
            pattern,
            target,
            str(path),
            file_glob or "",
            limit,
            offset,
        )
        task_data = _init_task_data(task_id)
        with _read_tracker_lock:
            if task_data["last_key"] == search_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = search_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

        if count >= 4:
            return json.dumps({
                "error": (
                    f"BLOCKED: You have run this exact search {count} times in a row. "
                    "The results have NOT changed. You already have this information. "
                    "STOP re-searching and proceed with your task."
                ),
                "pattern": pattern,
                "already_searched": count,
            }, ensure_ascii=False)

        expanded = os.path.expanduser(path)

        if target == "files":
            # Find files by glob pattern
            glob_pattern = os.path.join(expanded, "**", pattern)
            all_matches = sorted(
                glob_mod.glob(glob_pattern, recursive=True),
                key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                reverse=True,
            )
            # Apply pagination
            paginated = all_matches[offset : offset + limit]
            total_count = len(all_matches)

            result_dict: dict[str, Any] = {
                "matches": paginated,
                "count": len(paginated),
                "total_count": total_count,
                "pattern": pattern,
                "path": path,
                "truncated": total_count > offset + limit,
            }

            if count >= 3:
                result_dict["_warning"] = (
                    f"You have run this exact search {count} times consecutively. "
                    "The results have not changed. Use the information you already have."
                )

            result_json = json.dumps(result_dict, ensure_ascii=False)
            if result_dict.get("truncated"):
                next_offset = offset + limit
                result_json += (
                    f"\n\n[Hint: Results truncated. Use offset={next_offset} to see more, "
                    "or narrow with a more specific pattern or file_glob.]"
                )
            return result_json

        # Content search: regex grep
        regex = re.compile(pattern)
        results: list[dict[str, Any]] = []
        total_matches = 0

        for root, _dirs, files in os.walk(expanded):
            # Skip hidden directories
            _dirs[:] = [d for d in _dirs if not d.startswith(".")]
            for fname in sorted(files):
                # Apply file_glob filter
                if file_glob:
                    import fnmatch
                    if not fnmatch.fnmatch(fname, file_glob):
                        continue

                fpath = os.path.join(root, fname)

                # Skip binary files
                if has_binary_extension(fpath):
                    continue

                try:
                    with open(fpath, encoding="utf-8", errors="ignore") as fh:
                        file_lines = fh.readlines()
                except (OSError, UnicodeDecodeError):
                    continue

                file_matches: list[dict[str, Any]] = []
                for lineno, line in enumerate(file_lines, 1):
                    if regex.search(line):
                        total_matches += 1
                        if total_matches <= offset:
                            continue
                        if len(results) >= limit:
                            continue

                        if output_mode == "content":
                            match_entry: dict[str, Any] = {
                                "file": fpath,
                                "line": lineno,
                                "content": line.rstrip(),
                            }
                            # Add context lines if requested
                            if context > 0:
                                before = []
                                for ci in range(max(0, lineno - 1 - context), lineno - 1):
                                    before.append(f"{ci + 1}|{file_lines[ci].rstrip()}")
                                after = []
                                for ci in range(lineno, min(len(file_lines), lineno + context)):
                                    after.append(f"{ci + 1}|{file_lines[ci].rstrip()}")
                                if before:
                                    match_entry["before"] = before
                                if after:
                                    match_entry["after"] = after
                            results.append(match_entry)
                        elif output_mode == "count":
                            file_matches.append({
                                "line": lineno,
                                "content": line.rstrip(),
                            })
                        elif output_mode == "files_only":
                            # Just track that this file has a match
                            if not results or results[-1].get("file") != fpath:
                                results.append({"file": fpath})

                if output_mode == "count" and file_matches:
                    results.append({
                        "file": fpath,
                        "count": len(file_matches),
                    })

        result_dict = {
            "matches": results,
            "count": len(results),
            "total_matches": total_matches,
            "pattern": pattern,
            "path": path,
            "truncated": total_matches > offset + limit,
        }

        if count >= 3:
            result_dict["_warning"] = (
                f"You have run this exact search {count} times consecutively. "
                "The results have not changed. Use the information you already have."
            )

        result_json = json.dumps(result_dict, ensure_ascii=False)
        # Hint when results were truncated — explicit next offset is clearer
        # than relying on the model to infer it from total_count vs match count.
        if result_dict.get("truncated"):
            next_offset = offset + limit
            result_json += (
                f"\n\n[Hint: Results truncated. Use offset={next_offset} to see more, "
                "or narrow with a more specific pattern or file_glob.]"
            )
        return result_json
    except Exception as exc:
        return _tool_error(f"Search failed: {exc}")


async def _list_files_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Harness-local fallback: list directory contents.

    Delegates to search_files with target='files'.
    """
    path = arguments.get("path", ".")
    pattern = arguments.get("pattern", "*")
    limit = arguments.get("limit", 50)

    return await _search_files_handler(
        {"pattern": pattern, "target": "files", "path": path, "limit": limit},
        **kwargs,
    )
