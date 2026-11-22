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

import contextlib
import errno
import json
import os
import signal
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from surogates.tools.utils.process_tree import start_time

# Pod-local, off the s3fs ``/workspace`` mount, and writable by the non-root
# sandbox user (UID 1000).  ``/run`` is root-owned, so credential dirs live
# under ``/tmp`` with 0700 perms instead.
CODE_RUN_DIR = "/tmp/.code-runs"
# A pid or an exit code is short; a refreshed auth.json is a small JSON file.
_MAX_NUMBER_BYTES = 32
_MAX_PID = 2**31 - 1
_MAX_AUTH_BYTES = 1 << 20
# A folder held to work in: never through a link, and needing no read permission where O_PATH exists.
_HELD_FOLDER = (
    getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
# What a cleanup may spend, so commands adding files as fast as it removes them cannot hold it:
# inside the executor's 300 s, a number of removals no real run comes near, and failures in a row.
_CLEANUP_S = 120.0
_CLEANUP_STEPS = 5_000_000
_CLEANUP_FAILURES = 4000

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


def _open_run_file(base: str, run_id: str, *names: str) -> BinaryIO | None:
    """A regular file of the run's directory, open to read; None where there is none.

    Commands of the sandbox run as the same user and can change this
    directory, so the file is reached from the folder holding *base*, one
    folder at a time with no link followed, *base* included.  A file with a
    second name is refused too.  This keeps a poll from reading *through*
    what they put here; a file they move in is theirs to have put there.
    """
    from surogates.tools.workspace_io.local import _open_to_read

    held = os.path.abspath(base)
    root = os.path.realpath(os.path.dirname(held))
    try:
        opened = _open_to_read(root, os.path.join(root, os.path.basename(held), run_id, *names))
    # PermissionError: a link on the way, or a file whose mode was taken away.
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return None
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENXIO):  # there, but not a regular file (ENXIO: a socket)
            return None
        raise
    if os.fstat(opened.fileno()).st_nlink > 1:
        opened.close()
        return None
    return opened


def _read_number(base: str, run_id: str, name: str) -> int | None:
    """The number in the run's file *name*; None where there is no such file or no number in it."""
    try:
        opened = _open_run_file(base, run_id, name)
        if opened is None:
            return None
        with opened as fh:
            return int(fh.read(_MAX_NUMBER_BYTES).strip())
    except (ValueError, OSError):
        return None


def _read_pid(base: str, run_id: str) -> int:
    """The run's pid; -1 where there is none a process could have."""
    pid = _read_number(base, run_id, "pid")
    # Never 1: a process group of 1 is kill(-1), which signals every process the caller may signal.
    return pid if pid is not None and 1 < pid <= _MAX_PID else -1


def launch(payload: dict[str, Any], *, base: str = CODE_RUN_DIR) -> dict[str, Any]:
    """Spawn the vendor CLI detached and record its pid + log path.

    A start that is refused or fails leaves no run directory behind, with
    the credential it may already hold.  ``reports_cleaned`` tells the worker
    that this pod answers ``cleaned`` at a run's end.
    """
    run_dir = _run_dir(payload["run_id"], base)
    made: dict[str, Any] = {}
    try:
        answer = _launch(payload, base=base, made=made)
    except Exception as exc:
        # Answered, not raised: the executor's error envelope could not say whether the directory went.
        # A start that failed before it made the directory has nothing to remove.
        cleaned = _cleanup(run_dir, launched=True, identity=_run_identity(made)) if made else True
        # The kind of error only: its text could quote what it was handed, the credential included.
        error = f"could not start the run ({type(exc).__name__})"
        return {"ok": False, "run_id": payload["run_id"], "error": error, "cleaned": cleaned, "reports_cleaned": True}
    if not answer.get("ok"):
        answer["cleaned"] = _cleanup(run_dir, launched=True, identity=_run_identity(answer))
        answer.pop("run_dir", None)
    answer["reports_cleaned"] = True
    return answer


def _launch(payload: dict[str, Any], *, base: str, made: dict[str, Any]) -> dict[str, Any]:
    """The start itself; *made* is filled in as soon as the run's directory exists."""
    run_id = payload["run_id"]
    argv = list(payload["argv"])
    stdin = payload.get("stdin")
    env_overrides = dict(payload.get("env") or {})
    scrub = list(payload.get("scrub") or _DEFAULT_SCRUB)
    codex_auth_json = payload.get("codex_auth_json")

    run_dir = _run_dir(run_id, base)
    run_dir.mkdir(parents=True, exist_ok=True)
    made["created"] = True
    # Restrict the run dir so only the owner can read the injected credential
    # (mkdir mode is masked by umask, so chmod explicitly).
    try:
        os.chmod(run_dir, 0o700)
    except OSError:
        pass
    held = os.stat(run_dir, follow_symlinks=False)
    # Which directory this is: the name alone can be given to another one.
    identity = [held.st_dev, held.st_ino]
    made["run_dir"] = identity
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

    def close_stdin() -> None:
        if stdin_handle not in (subprocess.DEVNULL, None):
            stdin_handle.close()

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
            close_stdin()
            return {
                "ok": False,
                "run_id": run_id,
                "run_dir": identity,
                "error": "workdir must be inside WORKSPACE_DIR",
            }
        if not candidate_workdir.is_dir():
            close_stdin()
            return {
                "ok": False,
                "run_id": run_id,
                "run_dir": identity,
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
    except OSError as exc:  # its text names the program or the folder, which tells the user what to fix
        log_handle.close()
        close_stdin()
        return {"ok": False, "run_id": run_id, "run_dir": identity, "error": str(exc)}
    except ValueError as exc:
        # The kind of error only: the text of one about the environment can quote part of a value in it.
        log_handle.close()
        close_stdin()
        error = f"could not start the run ({type(exc).__name__})"
        return {"ok": False, "run_id": run_id, "run_dir": identity, "error": error}

    log_handle.close()
    close_stdin()

    pid_path.write_text(str(proc.pid))
    # What the worker keeps to stop this run and to clean up after it: the
    # files and names here are the sandbox's to change.
    return {"ok": True, "run_id": run_id, "pid": proc.pid, "start": start_time(proc.pid), "run_dir": identity}


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
    pid_path = run_dir / "pid"

    if not os.path.lexists(pid_path):
        # This poll is the run's last: whatever is left of it goes, its credential included.
        cleaned = _cleanup(run_dir, launched=bool(payload.get("launched")), identity=_run_identity(payload))
        return {
            "ok": False,
            "done": True,
            "new_output": "",
            "offset": offset,
            "exit_code": None,
            "error": f"unknown run {run_id!r}",
            "cleaned": cleaned,
        }

    new_output = ""
    new_offset = offset
    log = _open_run_file(base, run_id, "run.log")
    if log is not None:
        with log as fh:
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

    if os.path.lexists(exit_path):
        done = True
        exit_code = _read_exit_code(run_dir)
    else:
        pid = _read_pid(base, run_id)
        if pid <= 0 or not _pid_alive(pid):
            done = True
            exit_code = None  # died without recording an exit code

    codex_auth_json: str | None = None
    cleaned: bool | None = None
    if done:
        try:
            # Read any final bytes the marker race may have left.
            log = _open_run_file(base, run_id, "run.log")
            if log is not None:
                with log as fh:
                    fh.seek(new_offset)
                    tail = fh.read()
                    if tail:
                        new_output += tail.decode("utf-8", errors="replace")
                        new_offset += len(tail)
            # Read codex's (possibly refreshed) auth.json back before cleanup so
            # the worker can persist it to the vault.
            try:
                auth = _open_run_file(base, run_id, "codex", "auth.json")
                if auth is not None:
                    with auth as fh:
                        raw = fh.read(_MAX_AUTH_BYTES + 1)
                    if len(raw) <= _MAX_AUTH_BYTES:
                        codex_auth_json = raw.decode("utf-8")
            except (OSError, ValueError):
                codex_auth_json = None
        finally:
            # Whatever the run left there: the directory holds its credential.
            cleaned = _cleanup(run_dir, launched=bool(payload.get("launched")), identity=_run_identity(payload))

    result = {
        "ok": True,
        "done": done,
        "new_output": new_output,
        "offset": new_offset,
        "exit_code": exit_code,
    }
    if codex_auth_json is not None:
        result["codex_auth_json"] = codex_auth_json
    if cleaned is not None:
        result["cleaned"] = cleaned
    return result


def _read_exit_code(run_dir: Path) -> int | None:
    return _read_number(str(run_dir.parent), run_dir.name, "exit_code")


def cancel(payload: dict[str, Any], *, base: str = CODE_RUN_DIR) -> dict[str, Any]:
    """Stop the run's process group and remove its credential directory.

    The group is named by what ``launch`` answered (``pid`` and ``start``),
    which the worker kept.  Without them nothing is signalled.  ``cleaned``
    says whether the run's directory is gone.
    """
    run_id = payload["run_id"]
    run_dir = _run_dir(run_id, base)
    cleaned = False
    try:
        _stop_group(payload.get("pid"), payload.get("start"))
    finally:
        cleaned = _cleanup(run_dir, launched=bool(payload.get("launched")), identity=_run_identity(payload))
    return {"ok": True, "run_id": run_id, "cleaned": cleaned}


def _stop_group(pid: Any, started: Any) -> None:
    """Signal the process group *pid* leads, if it is still the process that started at *started*.

    A run is launched in a session of its own, so it leads its group.  The
    group is settled once: its leader may exit on the first signal while
    others in it still need the second.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or not 1 < pid <= _MAX_PID or started is None:
        return
    try:
        if start_time(pid) != started or os.getpgid(pid) != pid or pid == os.getpgrp():
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        return


def _run_identity(answer: dict[str, Any]) -> tuple[int, int] | None:
    """The ``(st_dev, st_ino)`` of the run's directory as ``launch`` named it, or None."""
    held = answer.get("run_dir")
    if isinstance(held, list) and len(held) == 2 and all(type(part) is int for part in held):
        return held[0], held[1]
    return None


def _cleanup(
    run_dir: Path, *, launched: bool = False, identity: tuple[int, int] | None = None,
) -> bool:
    """Remove the pod-local run directory (credentials included), by descriptor; whether it is gone.

    True means only this: the directory ``launch`` created was removed, and
    no file in it had a second name when it was removed.  It does not cover
    what left the directory before then, such as a copy or a moved folder.
    Two names for one file, both inside the directory, also answer False
    although the whole tree is gone: a false alarm, on the safe side.

    Commands can also work against this while it runs, so it goes on as long
    as the tree shrinks.  A link at the runs folder is refused: nothing
    legitimate puts one there, and what it leads to is not this run's.  With
    *launched* (the worker started this run), a directory that is not where
    it was made, or (by *identity*) is not the one that was made, counts as
    not removed, and is left alone.
    """
    parent, name = os.path.split(os.path.abspath(run_dir))
    known = launched or identity is not None
    try:
        runs = os.open(parent, _HELD_FOLDER)
    except FileNotFoundError:
        return not known
    except OSError:
        return False
    try:
        with contextlib.suppress(OSError):
            _own(runs)
        return _remove_tree(name, runs, known, identity)
    except OSError:
        return False
    finally:
        os.close(runs)


class _CleanupWork:
    """What a cleanup has spent (see :data:`_CLEANUP_S`), and whether a file it removed had another name."""

    def __init__(self) -> None:
        self.ends, self.steps, self.failures = time.monotonic() + _CLEANUP_S, 0, 0
        self.second_name = False

    def left(self) -> bool:
        return self.steps < _CLEANUP_STEPS and self.failures < _CLEANUP_FAILURES and time.monotonic() < self.ends

    def done(self) -> None:
        self.steps += 1
        self.failures = 0

    def failed(self) -> None:
        self.failures += 1
        time.sleep(0.001)  # a command changing modes in a loop gets no spin to race against


def _own(folder: int) -> None:
    """Give the owner back what it takes to list and empty the folder held as *folder*."""
    mode = stat.S_IMODE(os.fstat(folder).st_mode)
    if mode & 0o700 != 0o700:
        # By the descriptor's own path: fchmod refuses an O_PATH descriptor.
        os.chmod(f"/proc/self/fd/{folder}", mode | 0o700)


@contextlib.contextmanager
def _scan(folder: int) -> Iterator[Iterator[os.DirEntry]]:
    """The entries of the folder held as *folder*, read as they are asked for."""
    listed = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=folder)
    try:
        with os.scandir(listed) as entries:
            yield entries
    finally:
        os.close(listed)


def _remove_tree(name: str, parent: int, known: bool, identity: tuple[int, int] | None) -> bool:
    """Remove *name* in the folder held as *parent*, at any depth, with a fixed number of descriptors; whether it went.

    What is in a subfolder moves up into the top folder before the subfolder
    goes, so a tree deeper than the descriptor limit is removed too.  The top
    folder stays held across its removal: only its own link count says that
    this folder, and not another put at its name, is the one removed.
    """
    work = _CleanupWork()
    while work.left():
        try:
            top = os.open(name, _HELD_FOLDER, dir_fd=parent)
        except FileNotFoundError:
            return not known
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                if known:
                    return False  # not the directory that was made
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=parent)
            else:
                with contextlib.suppress(OSError):
                    _own(parent)
            work.failed()
            continue
        try:
            held = os.fstat(top)
            if identity is not None and (held.st_dev, held.st_ino) != identity:
                return False
            if not _empty(top, work):
                return False
            try:
                os.rmdir(name, dir_fd=parent)
            except FileNotFoundError:
                return os.fstat(top).st_nlink == 0 and not work.second_name if known else True
            except OSError:  # refilled, replaced, or its parent's mode taken away: looked at again
                with contextlib.suppress(OSError):
                    _own(parent)
                work.failed()
                continue
            if os.fstat(top).st_nlink == 0:
                return not work.second_name
            return not known  # what went was another folder put at the name
        finally:
            os.close(top)
    return False


def _empty(top: int, work: _CleanupWork) -> bool:
    """Empty the folder held as *top*; whether it was, before the cleanup's budget ran out."""
    while work.left():
        saw = shrank = False
        try:
            _own(top)
            with _scan(top) as entries:
                for entry in entries:
                    if not work.left():
                        return False
                    saw = True
                    try:
                        _take_one(entry.name, top, work)
                    except FileNotFoundError:
                        pass  # gone already: the tree is that much smaller
                    except OSError:
                        continue
                    shrank = True
                    work.done()
        except OSError:
            work.failed()
            continue
        if not saw:
            return True
        if not shrank:
            work.failed()
    return False


def _take_one(name: str, top: int, work: _CleanupWork) -> None:
    """Remove the file *name* in *top*; of a folder, move what it holds up into *top* and remove it."""
    found = os.stat(name, dir_fd=top, follow_symlinks=False)
    if not stat.S_ISDIR(found.st_mode):
        if stat.S_ISREG(found.st_mode) and found.st_nlink > 1:
            work.second_name = True
        os.unlink(name, dir_fd=top)
        return
    inner = os.open(name, _HELD_FOLDER, dir_fd=top)
    try:
        _own(inner)
        with _scan(inner) as entries:
            for entry in entries:
                if not work.left():
                    raise TimeoutError(errno.ETIMEDOUT, "the cleanup's budget ran out")
                # Moving a folder to another parent takes write permission on the folder itself.
                with contextlib.suppress(OSError):
                    if entry.is_dir(follow_symlinks=False):
                        child = os.open(entry.name, _HELD_FOLDER, dir_fd=inner)
                        try:
                            _own(child)
                        finally:
                            os.close(child)
                os.rename(entry.name, f".~{os.urandom(8).hex()}", src_dir_fd=inner, dst_dir_fd=top)
    finally:
        os.close(inner)
    os.rmdir(name, dir_fd=top)


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
