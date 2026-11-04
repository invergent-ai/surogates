"""Builtin terminal tool -- executes shell commands locally via subprocess.

Commands run through the session's WorkspaceIO (``run`` and ``start``); in the cloud sandbox that is a local subprocess.

Features:
- Execution with timeout and output capture
- ANSI escape stripping so the model never sees terminal formatting
- Output truncation (40 % head / 60 % tail split)
- Exit code interpretation for common CLI tools
- Working directory validation (allowlist-based)
- Background execution support
- Environment variable filtering via ``env_passthrough``

Registers the ``terminal`` tool with the tool registry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import traceback
from typing import Any, Optional

from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.utils.ansi_strip import strip_ansi
from surogates.tools.utils.tool_output_limits import get_max_bytes
from surogates.tools.utils.tool_result_storage import WORKSPACE_STORAGE_DIR, keep_out_of_git
from surogates.tools.utils.workspace_sandbox import NUL_REFUSED, WorkspaceSandboxError
from surogates.tools.utils.workspace_sandbox import holds_nul as _holds_nul
from surogates.tools.workspace_io import WorkspaceIO, workspace_io_from

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_OUTPUT_CHARS = 50_000
"""Maximum characters kept from command output before truncation."""

DISK_USAGE_WARNING_THRESHOLD_GB = float(
    os.getenv("TERMINAL_DISK_WARNING_GB", "500")
)
"""Disk usage threshold (GB) at which a warning is logged."""

_DEFAULT_TIMEOUT = int(os.getenv("TERMINAL_TIMEOUT", "180"))
"""Default command timeout in seconds."""


# ---------------------------------------------------------------------------
# Exit code interpretation
# ---------------------------------------------------------------------------

_EXIT_CODE_SEMANTICS: dict[str, dict[int, str]] = {
    "grep": {1: "No matches found (not an error)"},
    "egrep": {1: "No matches found (not an error)"},
    "fgrep": {1: "No matches found (not an error)"},
    "rg": {1: "No matches found (not an error)"},
    "ag": {1: "No matches found (not an error)"},
    "ack": {1: "No matches found (not an error)"},
    "diff": {1: "Files differ (expected, not an error)"},
    "colordiff": {1: "Files differ (expected, not an error)"},
    "find": {
        1: "Some directories were inaccessible (partial results may still be valid)"
    },
    "test": {1: "Condition evaluated to false (expected, not an error)"},
    "[": {1: "Condition evaluated to false (expected, not an error)"},
    "curl": {
        6: "Could not resolve host",
        7: "Failed to connect to host",
        22: "HTTP response code indicated error (e.g. 404, 500)",
        28: "Operation timed out",
    },
    "git": {
        1: "Non-zero exit (often normal -- e.g. 'git diff' returns 1 when files differ)"
    },
}


def _interpret_exit_code(command: str, exit_code: int) -> str | None:
    """Return a human-readable note when a non-zero exit code is non-erroneous.

    Returns None when the exit code is 0 or genuinely signals an error.
    The note is appended to the tool result so the model doesn't waste
    turns investigating expected exit codes.
    """
    if exit_code == 0:
        return None

    # Extract the last command in a pipeline/chain.
    segments = re.split(r"\s*(?:\|\||&&|[|;])\s*", command)
    last_segment = (segments[-1] if segments else command).strip()

    # Get base command name (first word), stripping env var assignments.
    words = last_segment.split()
    base_cmd = ""
    for w in words:
        if "=" in w and not w.startswith("-"):
            continue
        base_cmd = w.split("/")[-1]
        break

    if not base_cmd:
        return None

    cmd_semantics = _EXIT_CODE_SEMANTICS.get(base_cmd)
    if cmd_semantics and exit_code in cmd_semantics:
        return cmd_semantics[exit_code]

    return None




# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------

# Head/tail split of the character budget for over-long command output.
# Weighted to the tail because that is where a command reports what it
# concluded: the failing assertions, the stack trace, the summary line.  The
# head is kept at all because a command that dies during startup says so
# first, and that line is worth more than another page of test names.
_TRUNCATE_HEAD_FRACTION = 0.2


async def _spill_full_output(output: str, wio: WorkspaceIO) -> str | None:
    """Persist untruncated output so the model can go back for the middle.

    Written into the session workspace through *wio* -- the same WorkspaceIO
    ``read_file`` uses -- so the returned workspace-relative path is readable
    wherever the command ran: the sandbox pod, or the user's computer for a
    local-folder session, never this host's temp dir.  Returns ``None`` when
    no workspace is bound or the spill fails; a failed spill must never fail
    the command whose output it was trying to save.
    """
    if not wio.root:
        return None
    # Named after the output, not at random: a call resumed after its worker
    # stopped asks the computer for the same write, under the same name.
    digest = hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()[:32]
    path = f"{WORKSPACE_STORAGE_DIR}/terminal-output-{digest}.log"
    try:
        await keep_out_of_git(wio)
        await wio.write(
            await wio.resolve(path), output.encode("utf-8", errors="replace"),
        )
        return path
    except Exception:
        logger.debug("Failed to spill full terminal output", exc_info=True)
        return None


async def _truncate_output(output: str, wio: WorkspaceIO) -> str:
    """Cap output at the configured budget, keeping the head and the tail.

    The omitted middle is not lost: the full output is written into the
    workspace through *wio* and its path named in the notice, so recovering
    it costs one targeted read instead of re-running the command.
    """
    max_output_chars = get_max_bytes()
    if len(output) <= max_output_chars:
        return output

    head_chars = int(max_output_chars * _TRUNCATE_HEAD_FRACTION)
    tail_chars = max_output_chars - head_chars
    omitted = len(output) - head_chars - tail_chars

    full_path = await _spill_full_output(output, wio)
    recovery = (
        f" Full output: {full_path}" if full_path
        else " Re-run with a narrower command to see the omitted section."
    )
    truncated_notice = (
        f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted "
        f"out of {len(output)} total].{recovery} ...\n\n"
    )
    return output[:head_chars] + truncated_notice + output[-tail_chars:]




# ---------------------------------------------------------------------------
# Tool description
# ---------------------------------------------------------------------------

TERMINAL_TOOL_DESCRIPTION = (
    "Run a shell command in your Linux workspace (files persist between "
    "calls). Use it for scripts, builds, tests, git, package installs, data "
    "processing (e.g. python3) and network fetches. For reading, searching, "
    "listing and editing files use read_file, search_files, list_files, patch"
    " and write_file instead.\n"
    "Foreground commands return as soon as they finish; raise `timeout` "
    "(seconds) for long builds. background=true is only for servers or very "
    "long jobs (check them with the process tool; notify_on_complete=true "
    "reports when done). Interactive programs need pty=true. Pipe paging "
    "output (e.g. git log) to cat. Background processes may not survive "
    "between turns."
)

# Appended for a session on the user's computer (describe_for_device): its
# guest refuses these writes with a bare "Operation not permitted".
DEVICE_GIT_NOTE = (
    "In a shared folder on your computer, commands cannot write git's config or hooks, a `.git` itself, "
    "or the folder's shell, editor and agent settings (`.vscode`, `.idea`, `.mcp.json`, `.claude/commands`, "
    "`.claude/agents`, `.bashrc` and the like): such a write fails with \"Operation not permitted\". "
    "Commit, rebase, merge and cherry-pick work. Run `git init` and `git clone` in your home folder instead, "
    "and ask the user to run `git submodule update`, `git worktree add` and `git worktree remove` on their computer."
)

# Appended beside it: the guest serves a shared folder uncached, and refuses a shared
# mapping of its files (ENODEV) rather than let either side lose the other's writes.
DEVICE_MAPPING_NOTE = (
    "A program that maps a file in a shared folder for shared use, such as SQLite in WAL mode, fails there "
    "(SQLite says \"disk I/O error\"); keep such a database in your home folder. A database in a shared folder "
    "may use a rollback journal only while nothing on the user's computer has it open: locks do not cross the "
    "share, and writes from both sides at once are lost. A database already in WAL mode, including one a failed "
    "attempt switched, cannot be opened there: convert it on the user's computer, or delete it if your command "
    "just made it."
)


# ---------------------------------------------------------------------------
# Tool schema (exposed to the LLM)
# ---------------------------------------------------------------------------

TERMINAL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The shell command to run.",
        },
        "background": {
            "type": "boolean",
            "description": (
                (
                    "Run in the background (servers, watchers, very long jobs); default "
                    "false."
                )
            ),
            "default": False,
        },
        "timeout": {
            "type": "integer",
            "description": (
                (
                    "Max seconds to wait (default 180); the call returns as soon as the "
                    "command ends."
                )
            ),
            "minimum": 1,
        },
        "workdir": {
            "type": "string",
            "description": (
                "Working directory for this command (default the workspace)."
            ),
        },
        "check_interval": {
            "type": "integer",
            "description": (
                (
                    "Seconds between automatic progress reports for a background process "
                    "(messaging channels only, min 30)."
                )
            ),
            "minimum": 30,
        },
        "pty": {
            "type": "boolean",
            "description": (
                "Pseudo-terminal, for interactive CLI tools; default false."
            ),
            "default": False,
        },
        "notify_on_complete": {
            "type": "boolean",
            "description": (
                (
                    "With background=true: report automatically when the process finishes."
                )
            ),
            "default": False,
        },
    },
    "required": ["command"],
}


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------

def _blocked(error: str) -> str:
    """The result for a command refused before it ran."""
    return json.dumps(
        {"output": "", "exit_code": -1, "error": error, "status": "blocked"},
        ensure_ascii=False,
    )


async def _terminal_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Execute a shell command locally and return the JSON result.

    Steps:
    1. Parse arguments.
    2. Run through the workspace's ``run`` (or ``start``), which validates
       the workdir and sandboxes the command.
    3. Truncate output if needed.
    4. Interpret exit code.
    5. Return JSON result.
    """
    try:
        command = arguments.get("command", "")
        background = arguments.get("background", False)
        timeout = arguments.get("timeout") or _DEFAULT_TIMEOUT
        check_interval = arguments.get("check_interval")
        notify_on_complete = arguments.get("notify_on_complete", False)

        # The workspace validates and sandboxes the workdir; a refusal comes
        # back as WorkspaceSandboxError, worded for the model.
        requested_workdir = arguments.get("workdir")
        if _holds_nul(command, requested_workdir):
            # The model's mistake, answered as any command refused before it ran:
            # not a failure of the tool, with its traceback and its error log.
            return _blocked(NUL_REFUSED)
        wio = workspace_io_from(kwargs)

        # --- Background execution ------------------------------------------
        if background:
            try:
                started = await wio.start(
                    command,
                    workdir=requested_workdir,
                    task_id=kwargs.get("task_id", "default"),
                    pty=arguments.get("pty", False),
                    notify_on_complete=notify_on_complete,
                    watcher_interval=max(30, check_interval) if check_interval else None,
                )
            except WorkspaceSandboxError as exc:
                return _blocked(str(exc))

            result_data: dict[str, Any] = {
                "output": "Background process started",
                "session_id": started["session_id"],
                "pid": started["pid"],
                "exit_code": 0,
                "error": None,
            }

            if notify_on_complete:
                result_data["notify_on_complete"] = True

            if check_interval and check_interval < 30:
                result_data["check_interval_note"] = (
                    f"Requested {check_interval}s raised to minimum 30s"
                )

            return json.dumps(result_data, ensure_ascii=False)

        # --- Foreground execution ------------------------------------------
        # Run once: any exception other than a refused workdir means the
        # command may already have run, so it is not retried.
        try:
            result = await wio.run(command, workdir=requested_workdir, timeout=timeout)
        except WorkspaceSandboxError as exc:
            return _blocked(str(exc))

        # --- Post-process output -------------------------------------------
        output = await _truncate_output(result.output, wio)
        output = strip_ansi(output)
        output = output.strip() if output else ""

        exit_note = _interpret_exit_code(command, result.returncode)

        result_dict: dict[str, Any] = {
            "output": output,
            "exit_code": result.returncode,
            "error": None,
        }
        if exit_note:
            result_dict["exit_code_meaning"] = exit_note

        return json.dumps(result_dict, ensure_ascii=False)

    except Exception as exc:
        tb_str = traceback.format_exc()
        logger.error("terminal_tool exception:\n%s", tb_str)
        return json.dumps(
            {
                "output": "",
                "exit_code": -1,
                "error": f"Failed to execute command: {exc}",
                "traceback": tb_str,
                "status": "error",
            },
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(registry: ToolRegistry) -> None:
    """Register the terminal and process tools with the given registry."""
    from surogates.tools.utils.process_registry import (
        register as register_process,
    )

    registry.register(
        name="terminal",
        schema=ToolSchema(
            name="terminal",
            description=TERMINAL_TOOL_DESCRIPTION,
            parameters=TERMINAL_SCHEMA,
        ),
        handler=_terminal_handler,
        toolset="terminal",
        max_result_size=100_000,
    )

    register_process(registry)
