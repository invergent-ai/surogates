"""The cloud sandbox's workspace: this host's filesystem and shell.

What these methods do is what the tools did before WorkspaceIO existed.
Moving code here must not change what a cloud session sees.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import stat as stat_module
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from surogates.tools.utils.env_passthrough import is_env_passthrough
from surogates.tools.utils.process_registry import process_registry
from surogates.tools.utils.workspace_sandbox import (
    WorkspaceSandboxError,
    validate_path,
    validate_workdir,
)
from surogates.tools.workspace_io.base import FileStat, RunResult

logger = logging.getLogger(__name__)

# Spellings of "the home directory" a model uses for workdir; all mean the root.
_HOME_ALIASES = ("$HOME", "~", "$WORKSPACE_DIR", "${HOME}", "${WORKSPACE_DIR}")


# Writable dir for the generated ssh config, known_hosts and PATH wrappers.
# ``ssh`` reads its user config from the passwd home (read-only in the sandbox),
# so we can't rely on ``$HOME/.ssh/config``; the wrappers under ``bin/`` force
# ``-F <this dir>/config`` instead.
_SSH_DIR = "/tmp/surogates-ssh"


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
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _ALWAYS_INHERIT or is_env_passthrough(key):
            env[key] = value
    return env


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------

async def _run_command(
    command: str,
    *,
    cwd: str,
    timeout: int,
    env: dict[str, str],
) -> RunResult:
    """Run *command* via ``asyncio.create_subprocess_shell``."""
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
            return RunResult(
                output=f"Command timed out after {timeout} seconds",
                returncode=124,
                timed_out=True,
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        output = stdout
        if stderr:
            output = output + "\n" + stderr if output else stderr

        return RunResult(output=output, returncode=proc.returncode or 0)
    except Exception as exc:
        return RunResult(output=str(exc), returncode=-1)


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


class LocalWorkspaceIO:
    """WorkspaceIO over this host, contained to *workspace_path* when one is set."""

    def __init__(self, workspace_path: str | None = None) -> None:
        self.root = workspace_path or None

    async def resolve(self, path: str) -> str:
        if self.root:
            return validate_path(self.root, path)
        return str(Path(os.path.expanduser(path)).resolve())

    async def check_write(self, path: str) -> str | None:
        if _is_write_denied(path):
            return f"Write denied: '{path}' is a protected system/credential file."
        return _check_sensitive_path(path)

    async def stat(self, key: str) -> FileStat | None:
        try:
            st = os.stat(key)
        except (OSError, ValueError):
            return None
        return FileStat(
            is_dir=stat_module.S_ISDIR(st.st_mode), size=st.st_size, mtime=st.st_mtime,
        )

    async def read(self, key: str, max_bytes: int | None = None) -> bytes:
        with open(key, "rb") as fh:
            return fh.read(-1 if max_bytes is None else max_bytes)

    async def write(self, key: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(key) or ".", exist_ok=True)
        tmp = key + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            with contextlib.suppress(OSError):
                os.chmod(tmp, stat_module.S_IMODE(os.stat(key).st_mode))
            os.replace(tmp, key)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    async def delete(self, key: str) -> None:
        os.unlink(key)

    async def list_dir(self, key: str) -> list[str]:
        return os.listdir(key)

    @contextlib.asynccontextmanager
    async def local_file(self, key: str) -> AsyncIterator[Path]:
        yield Path(key)

    async def which(self, name: str) -> bool:
        return shutil.which(name) is not None

    def _workdir(self, requested: str | None) -> str:
        """The directory a command runs in: *requested*, contained to the workspace.

        Raises WorkspaceSandboxError, worded for the model, when it is not allowed.
        """
        if requested and self.root and requested in _HOME_ALIASES:
            requested = self.root
        if self.root:
            try:
                workdir = validate_workdir(self.root, requested)
            except WorkspaceSandboxError as exc:
                logger.warning(
                    "Blocked workdir outside workspace: %s (workspace: %s)",
                    str(requested)[:200],
                    self.root[:200],
                )
                raise WorkspaceSandboxError(
                    f"Blocked: {exc} All commands must run within "
                    "the workspace directory."
                ) from exc
        else:
            workdir = requested or _DEFAULT_CWD
        if workdir:
            error = _validate_workdir(workdir)
            if error:
                logger.warning("Blocked dangerous workdir: %s", workdir[:200])
                raise WorkspaceSandboxError(error)
        return workdir

    async def run(self, command: str, *, workdir: str | None, timeout: int) -> RunResult:
        cwd = self._workdir(workdir)
        env = _build_child_env()

        # Sandbox the environment when a workspace is set.
        # Override HOME so `cd ~`, `~/...` paths, and `$HOME` all resolve
        # inside the workspace.  Clear CDPATH to prevent `cd` from jumping
        # to directories outside the workspace.
        if self.root:
            env["HOME"] = self.root
            env.pop("CDPATH", None)
            # Prevent git from reading/writing config files that srt
            # blocks as mandatory deny paths (.gitconfig, .gitmodules).
            env["GIT_CONFIG_GLOBAL"] = "/dev/null"
            env["GIT_CONFIG_SYSTEM"] = "/dev/null"
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            # XDG config dir — redirect to workspace to avoid srt denials
            # on $HOME/.config/ access attempts.
            env["XDG_CONFIG_HOME"] = os.path.join(self.root, ".config")

        # Pin the ssh config/known_hosts into the child HOME for SSH-enabled
        # sessions (no-op otherwise).  Must happen before srt-wrapping so the
        # files exist when `ssh` reads them.
        _setup_ssh_home(env)

        # Wrap command with Anthropic Sandbox Runtime (srt) for OS-level
        # filesystem and network isolation via bubblewrap + seccomp.
        # This prevents shell escapes (cd ~, echo > /etc/passwd, etc.)
        # that application-level checks cannot catch.
        from surogates.config import load_settings

        if load_settings().sandbox.srt_enabled and self.root:
            command = _wrap_with_srt(command, self.root)

        return await _run_command(command, cwd=cwd, timeout=timeout, env=env)

    async def start(
        self,
        command: str,
        *,
        workdir: str | None,
        task_id: str,
        pty: bool,
        notify_on_complete: bool,
        watcher_interval: int | None,
    ) -> dict[str, Any]:
        cwd = self._workdir(workdir)
        env = _build_child_env()
        if self.root:
            env["HOME"] = self.root
            env.pop("CDPATH", None)
        _setup_ssh_home(env)
        session = process_registry.spawn(
            command=command, cwd=cwd, task_id=task_id, use_pty=pty, env_vars=env,
        )
        if notify_on_complete:
            session.notify_on_complete = True
        if watcher_interval:
            session.watcher_interval = watcher_interval
        return {"session_id": session.id, "pid": session.pid}
