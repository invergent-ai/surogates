"""Builtin terminal tool -- executes shell commands locally via subprocess.

Commands run directly via ``asyncio.create_subprocess_shell``.

Features:
- Local subprocess execution with timeout and output capture
- ANSI escape stripping so the model never sees terminal formatting
- Output truncation (40 % head / 60 % tail split)
- Exit code interpretation for common CLI tools
- Working directory validation (allowlist-based)
- Background execution support
- Transient error retry with exponential back-off
- Environment variable filtering via ``env_passthrough``

Registers the ``terminal`` tool with the tool registry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.utils.ansi_strip import strip_ansi
from surogates.tools.utils.env_passthrough import get_all_passthrough, is_env_passthrough
from surogates.tools.utils.process_registry import process_registry
from surogates.tools.utils.tool_output_limits import get_max_bytes
from surogates.tools.utils.workspace_sandbox import (
    WorkspaceSandboxError,
    validate_workdir as validate_workspace_workdir,
)

logger = logging.getLogger(__name__)

# Writable dir for the generated ssh config, known_hosts and PATH wrappers.
# ``ssh`` reads its user config from the passwd home (read-only in the sandbox),
# so we can't rely on ``$HOME/.ssh/config``; the wrappers under ``bin/`` force
# ``-F <this dir>/config`` instead.
_SSH_DIR = "/tmp/surogates-ssh"


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

_DEFAULT_CWD = os.getenv("TERMINAL_CWD", os.getcwd())
"""Default working directory for commands."""



# ---------------------------------------------------------------------------
# Anthropic Sandbox Runtime (srt) integration
# ---------------------------------------------------------------------------


def _ssh_hosts_from_env() -> list[str]:
    """Sorted SSH target hosts from the sandbox env, or empty when SSH is off.

    Presence of any host means the session is SSH-enabled: ``~/.ssh`` (which
    then holds only the non-secret config/known_hosts — the private key is in
    the isolated ssh-agent) becomes readable, and the hosts join the srt
    network allowlist.
    """
    raw = os.environ.get("SUROGATES_SSH_TARGETS", "")
    if not raw:
        return []
    try:
        targets = json.loads(raw)
    except ValueError:
        return []
    return sorted({str(t.get("host", "")) for t in targets if t.get("host")})


def _setup_ssh_home(child_env: dict[str, str]) -> None:
    """Install a writable ssh config + a PATH ``ssh`` wrapper that loads it.

    No-op unless the session is SSH-enabled (``SUROGATES_SSH_TARGETS`` set to a
    non-empty JSON list).  ``ssh`` reads its user config from the *passwd* home
    (read-only in the sandbox), not ``$HOME``, so a config written under the
    child's HOME is never loaded.  Instead we write config/known_hosts into a
    fixed writable dir and shadow ``ssh``/``scp``/``sftp`` on PATH with thin
    wrappers that force ``-F <config>``.  Rewriting on every command re-pins the
    strict host-key config so the agent cannot persistently weaken it.  All
    files are non-secret (the private key lives in the isolated ssh-agent).
    """
    raw = os.environ.get("SUROGATES_SSH_TARGETS", "")
    if not raw:
        return
    try:
        targets = json.loads(raw)
    except ValueError:
        return
    if not targets:
        return
    from surogates.ssh_access.resolve import build_ssh_config

    bin_dir = os.path.join(_SSH_DIR, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    os.chmod(_SSH_DIR, 0o700)

    known_hosts_path = os.path.join(_SSH_DIR, "known_hosts")
    with open(known_hosts_path, "w", encoding="utf-8") as fh:
        fh.write(os.environ.get("SUROGATES_SSH_KNOWN_HOSTS", ""))
    os.chmod(known_hosts_path, 0o600)

    config_path = os.path.join(_SSH_DIR, "config")
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(build_ssh_config(targets, known_hosts_path=known_hosts_path))
    os.chmod(config_path, 0o600)

    for tool in ("ssh", "scp", "sftp"):
        wrapper = os.path.join(bin_dir, tool)
        with open(wrapper, "w", encoding="utf-8") as fh:
            fh.write(
                f"#!/bin/sh\nexec /usr/bin/{tool} -F {config_path} \"$@\"\n",
            )
        os.chmod(wrapper, 0o755)

    child_env["PATH"] = bin_dir + ":" + child_env.get("PATH", "")


def _get_srt_settings_path(workspace_path: str) -> str:
    """Return the path to the per-workspace srt settings file.

    Creates the file if it doesn't exist.  The settings restrict writes
    to the workspace directory and block reads of secrets.  For SSH-enabled
    sessions the host allowlist and ``~/.ssh`` read policy differ, so the
    SSH host set feeds the settings-file hash to force a regenerate.
    """
    import hashlib

    ssh_hosts = _ssh_hosts_from_env()
    ssh_enabled = bool(ssh_hosts)
    seed = workspace_path + ("|ssh:" + ",".join(ssh_hosts) if ssh_enabled else "")
    ws_hash = hashlib.sha256(seed.encode()).hexdigest()[:12]
    from surogates.config import load_settings
    settings_dir = Path(load_settings().sandbox.srt_settings_dir)
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / f"srt-{ws_hash}.json"

    if not settings_path.exists():
        deny_read = [
            "~/.aws",
            "~/.gnupg",
            "~/.kube",
            "~/.docker",
            # /code run credentials live pod-local under
            # /tmp/.code-runs and in the vendor CLI config dirs.  Deny
            # reads so code the agent runs via the terminal can't
            # exfiltrate the user's coding-agent plan token.
            "/tmp/.code-runs",
            "auth.json",
            "$CODEX_HOME",
            "$CLAUDE_CONFIG_DIR",
        ]
        # Only deny ~/.ssh when SSH is NOT enabled.  With SSH enabled the dir
        # holds only the non-secret config + known_hosts (the private key never
        # touches the main container), and ssh must read them.
        if not ssh_enabled:
            deny_read.insert(0, "~/.ssh")
        allowed_domains = [
            "github.com",
            "*.github.com",
            "*.githubusercontent.com",
            "pypi.org",
            "*.pypi.org",
            "files.pythonhosted.org",
            "npmjs.org",
            "*.npmjs.org",
            "registry.npmjs.org",
            # Coding-agent (/code) vendor API endpoints.
            "api.anthropic.com",
            "api.openai.com",
            "chatgpt.com",
        ] + ssh_hosts
        settings = {
            "filesystem": {
                "denyRead": deny_read,
                "allowWrite": [workspace_path],
                "denyWrite": [
                    ".env",
                    ".env.local",
                    ".env.production",
                    "credentials.json",
                    "secrets.yaml",
                ],
            },
            "network": {
                "allowedDomains": allowed_domains,
                "deniedDomains": [],
            },
            "mandatoryDenySearchDepth": 3,
        }
        settings_path.write_text(
            json.dumps(settings, indent=2), encoding="utf-8"
        )

    return str(settings_path)


def _wrap_with_srt(command: str, workspace_path: str) -> str:
    """Wrap a shell command with the Anthropic Sandbox Runtime.

    Uses ``srt -c`` which passes the command string directly to a shell
    (like ``sh -c``), avoiding double-quoting issues.
    """
    settings_path = _get_srt_settings_path(workspace_path)
    escaped = command.replace("'", "'\\''")
    return f"srt --settings '{settings_path}' -c '{escaped}'"


# ---------------------------------------------------------------------------
# Working directory validation
# ---------------------------------------------------------------------------

_WORKDIR_SAFE_RE = re.compile(r"^[A-Za-z0-9/_\-.~ +@=,]+$")


def _validate_workdir(workdir: str) -> str | None:
    """Reject workdir values that don't look like a filesystem path.

    Uses an allowlist of safe characters rather than a deny-list, so novel
    shell metacharacters can't slip through.

    Returns None if safe, or an error message string if dangerous.
    """
    if not workdir:
        return None
    if not _WORKDIR_SAFE_RE.match(workdir):
        for ch in workdir:
            if not _WORKDIR_SAFE_RE.match(ch):
                return (
                    f"Blocked: workdir contains disallowed character {repr(ch)}. "
                    "Use a simple filesystem path without shell metacharacters."
                )
        return "Blocked: workdir contains disallowed characters."
    return None


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
# Environment variable filtering
# ---------------------------------------------------------------------------

# Variables that are always inherited by the child process regardless of
# passthrough config.  These are required for basic shell operation.
#
# ``PYTHONUSERBASE`` is included because the sandbox image installs pip
# packages into ``$PYTHONUSERBASE`` (off the s3fs mount — see
# images/sandbox/Dockerfile) and the agent's ``pip install X && python -c
# "import X"`` flow needs Python's site module to find them.  Without
# propagation, Python falls back to ``$HOME/.local`` — and we override
# HOME to the workspace below, which would point Python at the wrong
# (and s3fs-backed) location.
#
# ``UV_CACHE_DIR`` and ``XDG_CACHE_HOME`` are inherited for the same
# reason: uv's default cache is ``$XDG_CACHE_HOME/uv`` (else
# ``$HOME/.cache/uv``).  With our HOME override they would land on s3fs,
# and s3fs's locking/rename semantics deadlock uv mid-install.
_ALWAYS_INHERIT = frozenset({
    "HOME",
    # The isolated ssh-agent socket for SSH-enabled sessions; lets `ssh`
    # authenticate without ever seeing the private key.
    "SSH_AUTH_SOCK",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PYTHONUSERBASE",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "UV_CACHE_DIR",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
})


def _build_child_env() -> dict[str, str]:
    """Build a restricted environment dict for the child process.

    Starts from the current process environment but strips variables that
    are not in the always-inherit set or the passthrough allowlist.  This
    prevents secrets from leaking into commands the model runs.
    """
    passthrough = get_all_passthrough()
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _ALWAYS_INHERIT or is_env_passthrough(key):
            env[key] = value
    return env


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------

# Head/tail split of the character budget for over-long command output.
# Weighted to the tail because that is where a command reports what it
# concluded: the failing assertions, the stack trace, the summary line.  The
# head is kept at all because a command that dies during startup says so
# first, and that line is worth more than another page of test names.
_TRUNCATE_HEAD_FRACTION = 0.2


def _spill_full_output(output: str) -> str | None:
    """Persist untruncated output so the model can go back for the middle.

    Written to the local temp dir of whichever process ran the command --
    the sandbox pod for sandboxed sessions -- which is the same filesystem
    ``read_file`` and ``search_files`` resolve against, so the returned path
    is directly usable.  Returns ``None`` if the spill fails; a failed spill
    must never fail the command whose output it was trying to save.
    """
    try:
        fd, path = tempfile.mkstemp(prefix="terminal-output-", suffix=".log")
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(output)
        return path
    except OSError:
        logger.debug("Failed to spill full terminal output", exc_info=True)
        return None


def _truncate_output(output: str) -> str:
    """Cap output at the configured budget, keeping the head and the tail.

    The omitted middle is not lost: the full output is written to a file and
    its path named in the notice, so recovering it costs one targeted read
    instead of re-running the command.
    """
    max_output_chars = get_max_bytes()
    if len(output) <= max_output_chars:
        return output

    head_chars = int(max_output_chars * _TRUNCATE_HEAD_FRACTION)
    tail_chars = max_output_chars - head_chars
    omitted = len(output) - head_chars - tail_chars

    full_path = _spill_full_output(output)
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
# Subprocess execution
# ---------------------------------------------------------------------------

async def _run_command(
    command: str,
    *,
    cwd: str,
    timeout: int,
    env: dict[str, str],
) -> dict[str, Any]:
    """Run *command* via ``asyncio.create_subprocess_shell`` and return the result.

    Returns a dict with ``output`` (str) and ``returncode`` (int).
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "output": f"Command timed out after {timeout} seconds",
                "returncode": 124,
            }

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        output = stdout
        if stderr:
            output = output + "\n" + stderr if output else stderr

        return {"output": output, "returncode": proc.returncode or 0}
    except Exception as exc:
        return {"output": str(exc), "returncode": -1}


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

async def _terminal_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Execute a shell command locally and return the JSON result.

    Steps:
    1. Parse command, workdir, timeout from arguments.
    2. Validate workdir.
    3. Run command via ``asyncio.create_subprocess_shell`` with restricted env.
    4. Capture stdout + stderr with timeout.
    5. Truncate output if needed.
    6. Interpret exit code.
    7. Return JSON result.
    """
    try:
        command = arguments.get("command", "")
        background = arguments.get("background", False)
        timeout = arguments.get("timeout") or _DEFAULT_TIMEOUT
        check_interval = arguments.get("check_interval")
        notify_on_complete = arguments.get("notify_on_complete", False)

        # --- Resolve workdir (workspace-sandboxed) -------------------------
        # workspace_path from session config is the only trusted CWD.
        # User-supplied workdir is allowed only if it resolves inside
        # the workspace.
        workspace_path = kwargs.get("workspace_path")
        requested_workdir = arguments.get("workdir")

        # Normalize common shell variables to the workspace path.
        if requested_workdir and workspace_path:
            if requested_workdir in ("$HOME", "~", "$WORKSPACE_DIR", "${HOME}", "${WORKSPACE_DIR}"):
                requested_workdir = workspace_path

        if workspace_path:
            try:
                workdir = validate_workspace_workdir(
                    workspace_path,
                    requested_workdir,
                )
            except WorkspaceSandboxError as exc:
                logger.warning(
                    "Blocked workdir outside workspace: %s (workspace: %s)",
                    str(requested_workdir)[:200],
                    workspace_path[:200],
                )
                return json.dumps(
                    {
                        "output": "",
                        "exit_code": -1,
                        "error": (
                            f"Blocked: {exc} All commands must run within "
                            "the workspace directory."
                        ),
                        "status": "blocked",
                    },
                    ensure_ascii=False,
                )
        else:
            workdir = requested_workdir or _DEFAULT_CWD

        # --- Validate workdir characters -----------------------------------
        if workdir:
            workdir_error = _validate_workdir(workdir)
            if workdir_error:
                logger.warning(
                    "Blocked dangerous workdir: %s (command: %s)",
                    workdir[:200],
                    command[:200],
                )
                return json.dumps(
                    {
                        "output": "",
                        "exit_code": -1,
                        "error": workdir_error,
                        "status": "blocked",
                    },
                    ensure_ascii=False,
                )

        # --- Background execution ------------------------------------------
        if background:
            task_id = kwargs.get("task_id", "default")
            use_pty = arguments.get("pty", False)
            bg_env = _build_child_env()
            if workspace_path:
                bg_env["HOME"] = workspace_path
                bg_env.pop("CDPATH", None)
            _setup_ssh_home(bg_env)
            session = process_registry.spawn(
                command=command,
                cwd=workdir,
                task_id=task_id,
                use_pty=use_pty,
                env_vars=bg_env,
            )

            if notify_on_complete:
                session.notify_on_complete = True

            if check_interval:
                effective_interval = max(30, check_interval)
                session.watcher_interval = effective_interval

            result_data: dict[str, Any] = {
                "output": "Background process started",
                "session_id": session.id,
                "pid": session.pid,
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

        # --- Foreground execution with retry logic -------------------------
        child_env = _build_child_env()

        # Sandbox the environment when workspace_path is set.
        # Override HOME so `cd ~`, `~/...` paths, and `$HOME` all resolve
        # inside the workspace.  Clear CDPATH to prevent `cd` from jumping
        # to directories outside the workspace.
        if workspace_path:
            child_env["HOME"] = workspace_path
            child_env.pop("CDPATH", None)
            # Prevent git from reading/writing config files that srt
            # blocks as mandatory deny paths (.gitconfig, .gitmodules).
            child_env["GIT_CONFIG_GLOBAL"] = "/dev/null"
            child_env["GIT_CONFIG_SYSTEM"] = "/dev/null"
            child_env["GIT_CONFIG_NOSYSTEM"] = "1"
            # XDG config dir — redirect to workspace to avoid srt denials
            # on $HOME/.config/ access attempts.
            child_env["XDG_CONFIG_HOME"] = os.path.join(workspace_path, ".config")

        # Pin the ssh config/known_hosts into the child HOME for SSH-enabled
        # sessions (no-op otherwise).  Must happen before srt-wrapping so the
        # files exist when `ssh` reads them.
        _setup_ssh_home(child_env)

        # Wrap command with Anthropic Sandbox Runtime (srt) for OS-level
        # filesystem and network isolation via bubblewrap + seccomp.
        # This prevents shell escapes (cd ~, echo > /etc/passwd, etc.)
        # that application-level checks cannot catch.
        from surogates.config import load_settings as _load_settings
        if _load_settings().sandbox.srt_enabled and workspace_path:
            command = _wrap_with_srt(command, workspace_path)

        max_retries = 3
        retry_count = 0
        result: dict[str, Any] | None = None

        while retry_count <= max_retries:
            try:
                result = await _run_command(
                    command, cwd=workdir, timeout=timeout, env=child_env
                )
            except Exception as exc:
                error_str = str(exc).lower()
                if "timeout" in error_str:
                    return json.dumps(
                        {
                            "output": "",
                            "exit_code": 124,
                            "error": f"Command timed out after {timeout} seconds",
                        },
                        ensure_ascii=False,
                    )

                if retry_count < max_retries:
                    retry_count += 1
                    wait_time = 2 ** retry_count
                    logger.warning(
                        "Execution error, retrying in %ds (attempt %d/%d) "
                        "- Command: %s - Error: %s: %s",
                        wait_time,
                        retry_count,
                        max_retries,
                        command[:200],
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(wait_time)
                    continue

                logger.error(
                    "Execution failed after %d retries - Command: %s "
                    "- Error: %s: %s",
                    max_retries,
                    command[:200],
                    type(exc).__name__,
                    exc,
                )
                return json.dumps(
                    {
                        "output": "",
                        "exit_code": -1,
                        "error": (
                            f"Command execution failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    },
                    ensure_ascii=False,
                )

            break

        assert result is not None

        # --- Post-process output -------------------------------------------
        output = result.get("output", "")
        returncode = result.get("returncode", 0)

        output = _truncate_output(output)
        output = strip_ansi(output)
        output = output.strip() if output else ""

        exit_note = _interpret_exit_code(command, returncode)

        result_dict: dict[str, Any] = {
            "output": output,
            "exit_code": returncode,
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
