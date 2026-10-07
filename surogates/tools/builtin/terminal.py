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
from surogates.tools.utils.tool_result_storage import WORKSPACE_STORAGE_DIR
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
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

TERMINAL_TOOL_DESCRIPTION = """Execute shell commands on a Linux environment. Filesystem usually persists between calls.

Avoid this tool for reading, searching, listing, and editing files, unless the user explicitly asked for the shell command or you have established that the dedicated tool cannot do the job. Reach for the dedicated tool first:
  cat/head/tail to read a file — use read_file.
  grep/rg/find to search — use search_files.
  ls to list a directory — use search_files(target='files').
  sed/awk to edit a file — use patch.
  echo/cat heredoc to create a file — use write_file.
Reserve terminal for: builds, installs, git, processes, scripts, network, package managers, and anything that needs a shell.

Foreground (default): Commands return INSTANTLY when done, even if the timeout is high. Set timeout=300 for long builds/scripts — you'll still get the result in seconds if it's fast. Prefer foreground for short commands.
Background: Set background=true to get a session_id. Two patterns:
  (1) Long-lived processes that never exit (servers, watchers).
  (2) Long-running tasks with notify_on_complete=true — you can keep working on other things and the system auto-notifies you when the task finishes. Great for test suites, builds, deployments, or anything that takes more than a minute.
Use process(action="poll") for progress checks, process(action="wait") to block until done.
Working directory: Use 'workdir' for per-command cwd.
PTY mode: Set pty=true for interactive CLI tools (Codex, Claude Code, Python REPL).

Do NOT use vim/nano/interactive tools without pty=true — they hang without a pseudo-terminal. Pipe git output to cat if it might page.
Important: cloud sandboxes may be cleaned up, idled out, or recreated between turns. Persistent filesystem means files can resume later; it does NOT guarantee a continuously running machine or surviving background processes. Use terminal sandboxes for task work, not durable hosting.
"""

# Appended for a session on the user's computer (describe_for_device): its
# guest refuses these writes with a bare "Operation not permitted".
DEVICE_GIT_NOTE = (
    "In a shared folder on your computer, commands cannot write git's config or hooks, a `.git` itself, "
    "or the folder's shell, editor and agent settings (`.vscode`, `.idea`, `.mcp.json`, `.claude/commands`, "
    "`.claude/agents`, `.bashrc` and the like): such a write fails with \"Operation not permitted\". "
    "Commit, rebase, merge and cherry-pick work. Run `git init` and `git clone` in your home folder instead, "
    "and ask the user to run `git submodule update`, `git worktree add` and `git worktree remove` on their computer."
)


# ---------------------------------------------------------------------------
# Tool schema (exposed to the LLM)
# ---------------------------------------------------------------------------

TERMINAL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The command to execute on the VM",
        },
        "background": {
            "type": "boolean",
            "description": (
                "Run the command in the background. Two patterns: "
                "(1) Long-lived processes that never exit (servers, watchers). "
                "(2) Long-running tasks paired with notify_on_complete=true "
                "-- you can keep working and get notified when the task finishes. "
                "For short commands, prefer foreground with a generous timeout instead."
            ),
            "default": False,
        },
        "timeout": {
            "type": "integer",
            "description": (
                "Max seconds to wait (default: 180). Returns INSTANTLY when "
                "command finishes -- set high for long tasks, you won't wait "
                "unnecessarily."
            ),
            "minimum": 1,
        },
        "workdir": {
            "type": "string",
            "description": (
                "Working directory for this command (absolute path). "
                "Defaults to the session working directory."
            ),
        },
        "check_interval": {
            "type": "integer",
            "description": (
                "Seconds between automatic status checks for background "
                "processes (gateway/messaging only, minimum 30). When set, "
                "the system proactively reports progress."
            ),
            "minimum": 30,
        },
        "pty": {
            "type": "boolean",
            "description": (
                "Run in pseudo-terminal (PTY) mode for interactive CLI tools "
                "like Codex, Claude Code, or Python REPL. Default: false."
            ),
            "default": False,
        },
        "notify_on_complete": {
            "type": "boolean",
            "description": (
                "When true (and background=true), you'll be automatically "
                "notified when the process finishes -- no polling needed. "
                "Use this for tasks that take a while (tests, builds, "
                "deployments) so you can keep working on other things in "
                "the meantime."
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
