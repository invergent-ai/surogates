"""Pod-side launcher for /code runs (executes inside the sandbox pod).

Invoked by the ``tool-executor`` for the internal ``_code`` command.  It
spawns the vendor CLI **detached** (own session, survives the short exec that
launches it — the §6.3 self-supervision pattern that beats the ~305 s exec
ceiling), tails its log on subsequent ``poll`` calls, and kills it on
``cancel``.

Credentials are injected into the **child process environment only** (and,
for codex, a pod-local ``auth.json``) — never into the persistent pod env and
never into the workspace S3 mount.  The credential directory lives under
``/run/code`` (pod-local) and is removed on terminal poll / cancel.

State is kept on disk (pid/log/meta files) because every ``poll`` is a fresh
exec into the pod with no shared memory.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

# Pod-local, off the s3fs ``/workspace`` mount, and writable by the non-root
# sandbox user (UID 1000).  ``/run`` is root-owned, so credential dirs live
# under ``/tmp`` with 0700 perms instead.
CODE_RUN_DIR = "/tmp/.code-runs"

# Provider env vars scrubbed from the child by default so a stray value in the
# pod environment can't override the user's injected credential.  ``claude``
# precedence is ANTHROPIC_API_KEY > CLAUDE_CODE_OAUTH_TOKEN, so a leftover key
# would silently shadow a subscription token.
_DEFAULT_SCRUB = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
)


def _run_dir(run_id: str, base: str) -> Path:
    return Path(base) / run_id


def launch(payload: dict[str, Any], *, base: str = CODE_RUN_DIR) -> dict[str, Any]:
    """Spawn the vendor CLI detached and record its pid + log path."""
    run_id = payload["run_id"]
    argv = list(payload["argv"])
    stdin = payload.get("stdin")
    env_overrides = dict(payload.get("env") or {})
    scrub = list(payload.get("scrub") or _DEFAULT_SCRUB)
    codex_auth_json = payload.get("codex_auth_json")

    run_dir = _run_dir(run_id, base)
    run_dir.mkdir(parents=True, exist_ok=True)
    # Restrict the run dir so only the owner can read the injected credential
    # (mkdir mode is masked by umask, so chmod explicitly).
    try:
        os.chmod(run_dir, 0o700)
    except OSError:
        pass
    log_path = run_dir / "run.log"
    pid_path = run_dir / "pid"

    # Build the child environment: inherit the pod env, scrub conflicting
    # provider vars, then apply the injected credential env.
    child_env = dict(os.environ)
    for key in scrub:
        child_env.pop(key, None)

    if codex_auth_json is not None:
        codex_home = run_dir / "codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        auth_path = codex_home / "auth.json"
        auth_path.write_text(codex_auth_json)
        try:
            os.chmod(auth_path, 0o600)
        except OSError:
            pass
        child_env["CODEX_HOME"] = str(codex_home)

    child_env.update(env_overrides)

    stdin_handle = subprocess.DEVNULL
    if stdin is not None:
        stdin_path = run_dir / "stdin"
        stdin_path.write_text(stdin)
        stdin_handle = open(stdin_path, "rb")  # noqa: SIM115 — closed below

    workspace_dir = child_env.get("WORKSPACE_DIR", "/workspace")
    requested_workdir = payload.get("workdir") or workspace_dir
    if not os.path.isdir(workspace_dir):
        # No workspace mount (test/dev): inherit the launcher's cwd, and a
        # repo workdir cannot exist either.
        workdir = None
    else:
        workspace_root = Path(workspace_dir).resolve()
        candidate_workdir = Path(str(requested_workdir)).resolve()
        if (
            candidate_workdir != workspace_root
            and workspace_root not in candidate_workdir.parents
        ):
            return {
                "ok": False,
                "run_id": run_id,
                "error": "workdir must be inside WORKSPACE_DIR",
            }
        if not candidate_workdir.is_dir():
            return {
                "ok": False,
                "run_id": run_id,
                "error": f"workdir does not exist: {candidate_workdir}",
            }
        workdir = str(candidate_workdir)

    exit_path = run_dir / "exit_code"

    # Wrap the vendor argv in a shell that records the real exit code to a
    # marker file when the process finishes.  ``poll`` treats that marker as
    # the authoritative "done" signal — the launched process is detached
    # (own session) so we cannot ``waitpid`` it from a later fresh exec, and
    # a PID liveness probe is unreliable once the child is reparented/reaped.
    wrapped = [
        "/bin/sh",
        "-c",
        'rc=0; "$@" || rc=$?; printf %s "$rc" > "$0"; exit "$rc"',
        str(exit_path),
        *argv,
    ]

    log_handle = open(log_path, "wb")  # noqa: SIM115 — child owns it after spawn
    try:
        proc = subprocess.Popen(
            wrapped,
            stdin=stdin_handle,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=child_env,
            cwd=workdir,
            start_new_session=True,  # detach: survives the launching exec
        )
    except (OSError, ValueError) as exc:
        log_handle.close()
        if stdin_handle not in (subprocess.DEVNULL, None):
            stdin_handle.close()
        return {"ok": False, "run_id": run_id, "error": str(exc)}

    log_handle.close()
    if stdin_handle not in (subprocess.DEVNULL, None):
        stdin_handle.close()

    pid_path.write_text(str(proc.pid))
    return {"ok": True, "run_id": run_id, "pid": proc.pid}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def poll(payload: dict[str, Any], *, base: str = CODE_RUN_DIR) -> dict[str, Any]:
    """Return new log bytes since *offset* and whether the run has finished."""
    run_id = payload["run_id"]
    offset = int(payload.get("offset") or 0)

    run_dir = _run_dir(run_id, base)
    log_path = run_dir / "run.log"
    pid_path = run_dir / "pid"

    if not pid_path.exists():
        return {
            "ok": False,
            "done": True,
            "new_output": "",
            "offset": offset,
            "exit_code": None,
            "error": f"unknown run {run_id!r}",
        }

    new_output = ""
    new_offset = offset
    if log_path.exists():
        with open(log_path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
            new_output = chunk.decode("utf-8", errors="replace")
            new_offset = offset + len(chunk)

    # The exit-code marker is the authoritative "done" signal — it appears
    # only after the wrapped command finishes.  Fall back to a PID liveness
    # probe so a process killed before writing the marker (e.g. OOM, pod
    # reclaim) still resolves to done rather than hanging the poll loop.
    exit_path = run_dir / "exit_code"
    exit_code: int | None = None
    done = False

    if exit_path.exists():
        done = True
        exit_code = _read_exit_code(run_dir)
    else:
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            pid = -1
        if pid <= 0 or not _pid_alive(pid):
            done = True
            exit_code = None  # died without recording an exit code

    codex_auth_json: str | None = None
    if done:
        # Read any final bytes the marker race may have left.
        if log_path.exists():
            with open(log_path, "rb") as fh:
                fh.seek(new_offset)
                tail = fh.read()
                if tail:
                    new_output += tail.decode("utf-8", errors="replace")
                    new_offset += len(tail)
        # Read codex's (possibly refreshed) auth.json back before cleanup so
        # the worker can persist it to the vault.
        codex_auth_path = run_dir / "codex" / "auth.json"
        if codex_auth_path.exists():
            try:
                codex_auth_json = codex_auth_path.read_text()
            except OSError:
                codex_auth_json = None
        _cleanup(run_dir)

    result = {
        "ok": True,
        "done": done,
        "new_output": new_output,
        "offset": new_offset,
        "exit_code": exit_code,
    }
    if codex_auth_json is not None:
        result["codex_auth_json"] = codex_auth_json
    return result


def _read_exit_code(run_dir: Path) -> int | None:
    marker = run_dir / "exit_code"
    if marker.exists():
        try:
            return int(marker.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def cancel(payload: dict[str, Any], *, base: str = CODE_RUN_DIR) -> dict[str, Any]:
    """Kill the run's process group and remove its credential directory."""
    run_id = payload["run_id"]
    run_dir = _run_dir(run_id, base)
    pid_path = run_dir / "pid"

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            pid = -1
        if pid > 0:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(pid), sig)
                except (ProcessLookupError, PermissionError):
                    break
    _cleanup(run_dir)
    return {"ok": True, "run_id": run_id}


def _cleanup(run_dir: Path) -> None:
    """Remove the pod-local run directory (credentials included)."""
    import shutil

    shutil.rmtree(run_dir, ignore_errors=True)


def checkout(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a git checkout script with a private per-call env (the git PAT).

    Runs in ``WORKSPACE_DIR`` so the clone lands beside the coding run's
    workspace.  Output is redacted and capped so a leaked token can't surface
    in the returned payload.
    """
    from surogates.harness.redact import redact_sensitive_text

    run_id = payload.get("run_id", "")
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        return {"ok": False, "run_id": run_id, "error": "checkout command is required"}
    child_env = dict(os.environ)
    child_env.update(dict(payload.get("env") or {}))
    workspace_dir = child_env.get("WORKSPACE_DIR", "/workspace")
    workspace_root = Path(workspace_dir).resolve()
    if not workspace_root.is_dir():
        return {
            "ok": False, "run_id": run_id,
            "error": f"workspace missing: {workspace_root}",
        }
    proc = subprocess.run(  # noqa: S603 — fixed shell, no untrusted argv
        ["/bin/sh", "-lc", command],
        cwd=str(workspace_root),
        env=child_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    ok = proc.returncode == 0
    stdout = redact_sensitive_text((proc.stdout or "")[-4000:])
    stderr = redact_sensitive_text((proc.stderr or "")[-4000:])
    return {
        "ok": ok,
        "run_id": run_id,
        "exit_code": proc.returncode,
        "output": stdout,
        "error": None if ok else (stderr or stdout or "checkout failed"),
    }


def dispatch(payload: dict[str, Any], *, base: str = CODE_RUN_DIR) -> dict[str, Any]:
    """Route a ``_code`` payload to launch / poll / cancel / checkout."""
    action = payload.get("action")
    if action == "launch":
        return launch(payload, base=base)
    if action == "poll":
        return poll(payload, base=base)
    if action == "cancel":
        return cancel(payload, base=base)
    if action == "checkout":
        return checkout(payload)
    return {"ok": False, "error": f"unknown action {action!r}"}


def main(argv: list[str]) -> None:
    """CLI entry used by the pod tool-executor: ``_code '<json payload>'``."""
    raw = argv[1] if len(argv) > 1 else "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"bad payload: {exc}"}))
        return
    print(json.dumps(dispatch(payload)))


if __name__ == "__main__":  # pragma: no cover
    import sys

    main(sys.argv)
