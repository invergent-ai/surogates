"""Process Registry -- In-memory registry for managed background processes.

Tracks processes spawned via terminal(background=true), providing:
  - Output buffering (rolling 200KB window)
  - Status polling and log retrieval
  - Blocking wait with interrupt support
  - Process killing
  - Crash recovery via JSON checkpoint file
  - Session-scoped tracking for gateway reset protection

Background processes execute locally via ``subprocess.Popen``.

Usage::

    from surogates.tools.utils.process_registry import process_registry

    # Spawn a background process (called from terminal_tool)
    session = process_registry.spawn(command="pytest -v", task_id="task_123")

    # Poll for status
    result = process_registry.poll(session.id)

    # Block until done
    result = process_registry.wait(session.id, timeout=300)

    # Kill it
    process_registry.kill_process(session.id)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue as _queue_mod
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from surogates.tools.utils.ansi_strip import strip_ansi

_IS_WINDOWS = platform.system() == "Windows"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Checkpoint file for crash recovery
# ---------------------------------------------------------------------------

_SUROGATES_HOME = Path(os.environ.get("SUROGATES_HOME", Path.home() / ".surogates"))

CHECKPOINT_PATH = _SUROGATES_HOME / "processes.json"

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_OUTPUT_CHARS = 200_000      # 200KB rolling output buffer
FINISHED_TTL_SECONDS = 1800     # Keep finished processes for 30 minutes
MAX_PROCESSES = 64              # Max concurrent tracked processes (LRU pruning)


# ---------------------------------------------------------------------------
# Shell discovery
# ---------------------------------------------------------------------------

def _find_shell() -> str:
    """Return the path to the user's preferred shell.

    Checks ``$SHELL``, then falls back to ``/bin/bash`` on Unix or
    ``cmd.exe`` on Windows.
    """
    shell = os.environ.get("SHELL")
    if shell and os.path.isfile(shell):
        return shell
    if _IS_WINDOWS:
        return os.environ.get("COMSPEC", "cmd.exe")
    return "/bin/bash"


def _sanitize_subprocess_env(
    base_env: os._Environ | dict[str, str],
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a clean environment dict for subprocess execution.

    Starts from *base_env*, overlays *extra*, and removes any keys whose
    values are ``None``.
    """
    env = dict(base_env)
    if extra:
        env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


# ---------------------------------------------------------------------------
# Atomic JSON write helper
# ---------------------------------------------------------------------------

def _atomic_json_write(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically via a temporary file.

    Creates parent directories if they do not exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=".proc_"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# ProcessSession dataclass
# ---------------------------------------------------------------------------

@dataclass
class ProcessSession:
    """A tracked background process with output buffering."""

    id: str                                     # Unique session ID ("proc_xxxxxxxxxxxx")
    command: str                                 # Original command string
    task_id: str = ""                           # Task/sandbox isolation key
    session_key: str = ""                       # Session key (for reset protection)
    pid: Optional[int] = None                   # OS process ID
    process: Optional[subprocess.Popen] = None  # Popen handle
    cwd: Optional[str] = None                   # Working directory
    started_at: float = 0.0                     # time.time() of spawn
    exited: bool = False                        # Whether the process has finished
    exit_code: Optional[int] = None             # Exit code (None if still running)
    output_buffer: str = ""                     # Rolling output (last MAX_OUTPUT_CHARS)
    max_output_chars: int = MAX_OUTPUT_CHARS
    detached: bool = False                      # True if recovered from crash (no pipe)
    pid_scope: str = "host"                     # "host" for local PIDs
    # Watcher/notification metadata (persisted for crash recovery)
    watcher_platform: str = ""
    watcher_chat_id: str = ""
    watcher_thread_id: str = ""
    watcher_interval: int = 0                   # 0 = no watcher configured
    notify_on_complete: bool = False             # Queue agent notification on exit
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _pty: Any = field(default=None, repr=False)  # ptyprocess handle (when use_pty=True)


# ---------------------------------------------------------------------------
# ProcessRegistry
# ---------------------------------------------------------------------------

class ProcessRegistry:
    """In-memory registry of running and finished background processes.

    Thread-safe.  Accessed from:
      - Executor threads (terminal_tool, process tool handlers)
      - Asyncio loop (watcher tasks, session reset checks)
      - Cleanup thread (process reaping coordination)
    """

    _SHELL_NOISE_SUBSTRINGS = (
        "bash: cannot set terminal process group",
        "bash: no job control in this shell",
        "no job control in this shell",
        "cannot set terminal process group",
        "tcsetattr: Inappropriate ioctl for device",
    )

    def __init__(self) -> None:
        self._running: Dict[str, ProcessSession] = {}
        self._finished: Dict[str, ProcessSession] = {}
        self._lock = threading.Lock()

        # Side-channel for check_interval watchers
        self.pending_watchers: List[Dict[str, Any]] = []

        # Completion notifications -- processes with notify_on_complete push
        # here on exit.  The harness loop drains this after each agent turn to
        # auto-trigger a new agent turn with the process results.
        self.completion_queue: _queue_mod.Queue = _queue_mod.Queue()

    # ----- Helpers -----

    @staticmethod
    def _clean_shell_noise(text: str) -> str:
        """Strip shell startup warnings from the beginning of output."""
        lines = text.split("\n")
        while lines and any(
            noise in lines[0]
            for noise in ProcessRegistry._SHELL_NOISE_SUBSTRINGS
        ):
            lines.pop(0)
        return "\n".join(lines)

    @staticmethod
    def _is_host_pid_alive(pid: Optional[int]) -> bool:
        """Best-effort liveness check for host-visible PIDs."""
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _refresh_detached_session(
        self, session: Optional[ProcessSession]
    ) -> Optional[ProcessSession]:
        """Update recovered host-PID sessions when the process has exited."""
        if (
            session is None
            or session.exited
            or not session.detached
            or session.pid_scope != "host"
        ):
            return session

        if self._is_host_pid_alive(session.pid):
            return session

        with session._lock:
            if session.exited:
                return session
            session.exited = True
            # Recovered sessions no longer have a waitable handle, so the
            # real exit code is unavailable.
            session.exit_code = None

        self._move_to_finished(session)
        return session

    @staticmethod
    def _terminate_host_pid(pid: int) -> None:
        """Terminate a host-visible PID without the original process handle."""
        if _IS_WINDOWS:
            os.kill(pid, signal.SIGTERM)
            return

        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)

    # ----- Spawn -----

    def spawn(
        self,
        command: str,
        cwd: str | None = None,
        task_id: str = "",
        session_key: str = "",
        env_vars: dict | None = None,
        use_pty: bool = False,
    ) -> ProcessSession:
        """Spawn a background process locally.

        Args:
            command: Shell command to execute.
            cwd: Working directory. Defaults to ``os.getcwd()``.
            task_id: Task isolation key.
            session_key: Session key (for reset protection).
            env_vars: Extra environment variables to set.
            use_pty: If True, use a pseudo-terminal via ptyprocess for
                interactive CLI tools. Falls back to ``subprocess.Popen``
                if ptyprocess is not installed.
        """
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            task_id=task_id,
            session_key=session_key,
            cwd=cwd or os.getcwd(),
            started_at=time.time(),
        )

        if use_pty:
            # Try PTY mode for interactive CLI tools
            try:
                if _IS_WINDOWS:
                    from winpty import PtyProcess as _PtyProcessCls
                else:
                    from ptyprocess import PtyProcess as _PtyProcessCls
                user_shell = _find_shell()
                pty_env = _sanitize_subprocess_env(os.environ, env_vars)
                pty_env["PYTHONUNBUFFERED"] = "1"
                pty_proc = _PtyProcessCls.spawn(
                    [user_shell, "-lic", command],
                    cwd=session.cwd,
                    env=pty_env,
                    dimensions=(30, 120),
                )
                session.pid = pty_proc.pid
                # Store the pty handle on the session for read/write
                session._pty = pty_proc

                # PTY reader thread
                reader = threading.Thread(
                    target=self._pty_reader_loop,
                    args=(session,),
                    daemon=True,
                    name=f"proc-pty-reader-{session.id}",
                )
                session._reader_thread = reader
                reader.start()

                with self._lock:
                    self._prune_if_needed()
                    self._running[session.id] = session

                self._write_checkpoint()
                return session

            except ImportError:
                logger.warning(
                    "ptyprocess not installed, falling back to pipe mode"
                )
            except Exception as e:
                logger.warning(
                    "PTY spawn failed (%s), falling back to pipe mode", e
                )

        # Standard Popen path (non-PTY or PTY fallback)
        user_shell = _find_shell()
        # Force unbuffered output for Python scripts so progress is visible
        # during background execution.
        bg_env = _sanitize_subprocess_env(os.environ, env_vars)
        bg_env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [user_shell, "-lic", command],
            text=True,
            cwd=session.cwd,
            env=bg_env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            preexec_fn=None if _IS_WINDOWS else os.setsid,
        )

        session.process = proc
        session.pid = proc.pid

        # Start output reader thread
        reader = threading.Thread(
            target=self._reader_loop,
            args=(session,),
            daemon=True,
            name=f"proc-reader-{session.id}",
        )
        session._reader_thread = reader
        reader.start()

        with self._lock:
            self._prune_if_needed()
            self._running[session.id] = session

        self._write_checkpoint()
        return session

    # ----- Reader / Poller Threads -----

    def _reader_loop(self, session: ProcessSession) -> None:
        """Background thread: read stdout from a local Popen process."""
        first_chunk = True
        try:
            while True:
                chunk = session.process.stdout.read(4096)
                if not chunk:
                    break
                if first_chunk:
                    chunk = self._clean_shell_noise(chunk)
                    first_chunk = False
                with session._lock:
                    session.output_buffer += chunk
                    if len(session.output_buffer) > session.max_output_chars:
                        session.output_buffer = session.output_buffer[
                            -session.max_output_chars :
                        ]
        except Exception as e:
            logger.debug("Process stdout reader ended: %s", e)

        # Process exited
        try:
            session.process.wait(timeout=5)
        except Exception as e:
            logger.debug("Process wait timed out or failed: %s", e)
        session.exited = True
        session.exit_code = session.process.returncode
        self._move_to_finished(session)

    def _pty_reader_loop(self, session: ProcessSession) -> None:
        """Background thread: read output from a PTY process."""
        pty = session._pty
        try:
            while pty.isalive():
                try:
                    chunk = pty.read(4096)
                    if chunk:
                        # ptyprocess returns bytes
                        text = (
                            chunk
                            if isinstance(chunk, str)
                            else chunk.decode("utf-8", errors="replace")
                        )
                        with session._lock:
                            session.output_buffer += text
                            if (
                                len(session.output_buffer)
                                > session.max_output_chars
                            ):
                                session.output_buffer = session.output_buffer[
                                    -session.max_output_chars :
                                ]
                except EOFError:
                    break
                except Exception:
                    break
        except Exception as e:
            logger.debug("PTY stdout reader ended: %s", e)

        # Process exited
        try:
            pty.wait()
        except Exception as e:
            logger.debug("PTY wait timed out or failed: %s", e)
        session.exited = True
        session.exit_code = (
            pty.exitstatus if hasattr(pty, "exitstatus") else -1
        )
        self._move_to_finished(session)

    def _move_to_finished(self, session: ProcessSession) -> None:
        """Move a session from running to finished."""
        with self._lock:
            self._running.pop(session.id, None)
            self._finished[session.id] = session
        self._write_checkpoint()

        # If the caller requested agent notification, enqueue the completion
        # so the harness can auto-trigger a new agent turn.
        if session.notify_on_complete:
            output_tail = (
                strip_ansi(session.output_buffer[-2000:])
                if session.output_buffer
                else ""
            )
            self.completion_queue.put(
                {
                    "session_id": session.id,
                    "command": session.command,
                    "exit_code": session.exit_code,
                    "output": output_tail,
                }
            )

    # ----- Query Methods -----

    def get(self, session_id: str) -> Optional[ProcessSession]:
        """Get a session by ID (running or finished)."""
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(
                session_id
            )
        return self._refresh_detached_session(session)

    def poll(self, session_id: str) -> dict:
        """Check status and get new output for a background process."""
        session = self.get(session_id)
        if session is None:
            return {
                "status": "not_found",
                "error": f"No process with ID {session_id}",
            }

        with session._lock:
            output_preview = (
                strip_ansi(session.output_buffer[-1000:])
                if session.output_buffer
                else ""
            )

        result = {
            "session_id": session.id,
            "command": session.command,
            "status": "exited" if session.exited else "running",
            "pid": session.pid,
            "uptime_seconds": int(time.time() - session.started_at),
            "output_preview": output_preview,
        }
        if session.exited:
            result["exit_code"] = session.exit_code
        if session.detached:
            result["detached"] = True
            result["note"] = (
                "Process recovered after restart -- output history unavailable"
            )
        return result

    def read_log(
        self, session_id: str, offset: int = 0, limit: int = 200
    ) -> dict:
        """Read the full output log with optional pagination by lines."""
        session = self.get(session_id)
        if session is None:
            return {
                "status": "not_found",
                "error": f"No process with ID {session_id}",
            }

        with session._lock:
            full_output = strip_ansi(session.output_buffer)

        lines = full_output.splitlines()
        total_lines = len(lines)

        # Default: last N lines
        if offset == 0 and limit > 0:
            selected = lines[-limit:]
        else:
            selected = lines[offset : offset + limit]

        return {
            "session_id": session.id,
            "status": "exited" if session.exited else "running",
            "output": "\n".join(selected),
            "total_lines": total_lines,
            "showing": f"{len(selected)} lines",
        }

    def wait(
        self,
        session_id: str,
        timeout: int | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> dict:
        """Block until a process exits, timeout, or interrupt.

        Args:
            session_id: The process to wait for.
            timeout: Max seconds to block.  Defaults to ``TERMINAL_TIMEOUT``
                env var (180s).
            interrupt_event: Optional threading event that, when set, causes
                the wait to return early with an ``"interrupted"`` status.

        Returns:
            dict with status (``"exited"``, ``"timeout"``, ``"interrupted"``,
            ``"not_found"``) and output snapshot.
        """
        default_timeout = int(os.getenv("TERMINAL_TIMEOUT", "180"))
        max_timeout = default_timeout
        requested_timeout = timeout
        timeout_note: str | None = None

        if requested_timeout and requested_timeout > max_timeout:
            effective_timeout = max_timeout
            timeout_note = (
                f"Requested wait of {requested_timeout}s was clamped "
                f"to configured limit of {max_timeout}s"
            )
        else:
            effective_timeout = requested_timeout or max_timeout

        session = self.get(session_id)
        if session is None:
            return {
                "status": "not_found",
                "error": f"No process with ID {session_id}",
            }

        deadline = time.monotonic() + effective_timeout

        while time.monotonic() < deadline:
            session = self._refresh_detached_session(session)
            if session.exited:
                result: dict[str, Any] = {
                    "status": "exited",
                    "exit_code": session.exit_code,
                    "output": strip_ansi(session.output_buffer[-2000:]),
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result

            if interrupt_event is not None and interrupt_event.is_set():
                result = {
                    "status": "interrupted",
                    "output": strip_ansi(session.output_buffer[-1000:]),
                    "note": "User sent a new message -- wait interrupted",
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result

            time.sleep(1)

        result = {
            "status": "timeout",
            "output": strip_ansi(session.output_buffer[-1000:]),
        }
        if timeout_note:
            result["timeout_note"] = timeout_note
        else:
            result["timeout_note"] = (
                f"Waited {effective_timeout}s, process still running"
            )
        return result

    def kill_process(self, session_id: str) -> dict:
        """Kill a background process."""
        session = self.get(session_id)
        if session is None:
            return {
                "status": "not_found",
                "error": f"No process with ID {session_id}",
            }

        if session.exited:
            return {
                "status": "already_exited",
                "exit_code": session.exit_code,
            }

        # Kill via PTY, or Popen handle, or raw PID (detached)
        try:
            if session._pty:
                # PTY process -- terminate via ptyprocess
                try:
                    session._pty.terminate(force=True)
                except Exception:
                    if session.pid:
                        os.kill(session.pid, signal.SIGTERM)
            elif session.process:
                # Local process -- kill the process group
                try:
                    if _IS_WINDOWS:
                        session.process.terminate()
                    else:
                        os.killpg(
                            os.getpgid(session.process.pid), signal.SIGTERM
                        )
                except (ProcessLookupError, PermissionError):
                    session.process.kill()
            elif (
                session.detached
                and session.pid_scope == "host"
                and session.pid
            ):
                if not self._is_host_pid_alive(session.pid):
                    with session._lock:
                        session.exited = True
                        session.exit_code = None
                    self._move_to_finished(session)
                    return {
                        "status": "already_exited",
                        "exit_code": session.exit_code,
                    }
                self._terminate_host_pid(session.pid)
            else:
                return {
                    "status": "error",
                    "error": (
                        "Recovered process cannot be killed after restart "
                        "because its original runtime handle is no longer "
                        "available"
                    ),
                }
            session.exited = True
            session.exit_code = -15  # SIGTERM
            self._move_to_finished(session)
            self._write_checkpoint()
            return {"status": "killed", "session_id": session.id}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def write_stdin(self, session_id: str, data: str) -> dict:
        """Send raw data to a running process's stdin (no newline appended)."""
        session = self.get(session_id)
        if session is None:
            return {
                "status": "not_found",
                "error": f"No process with ID {session_id}",
            }
        if session.exited:
            return {
                "status": "already_exited",
                "error": "Process has already finished",
            }

        # PTY mode -- write through pty handle (expects bytes)
        if hasattr(session, "_pty") and session._pty:
            try:
                pty_data = (
                    data.encode("utf-8") if isinstance(data, str) else data
                )
                session._pty.write(pty_data)
                return {"status": "ok", "bytes_written": len(data)}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # Popen mode -- write through stdin pipe
        if not session.process or not session.process.stdin:
            return {
                "status": "error",
                "error": "Process stdin not available (stdin closed)",
            }
        try:
            session.process.stdin.write(data)
            session.process.stdin.flush()
            return {"status": "ok", "bytes_written": len(data)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def submit_stdin(self, session_id: str, data: str = "") -> dict:
        """Send data + newline to a running process's stdin (like pressing Enter)."""
        return self.write_stdin(session_id, data + "\n")

    def list_sessions(self, task_id: str | None = None) -> list:
        """List all running and recently-finished processes."""
        with self._lock:
            all_sessions = list(self._running.values()) + list(
                self._finished.values()
            )

        all_sessions = [
            self._refresh_detached_session(s) for s in all_sessions
        ]

        if task_id:
            all_sessions = [
                s for s in all_sessions if s.task_id == task_id
            ]

        result = []
        for s in all_sessions:
            entry: dict[str, Any] = {
                "session_id": s.id,
                "command": s.command[:200],
                "cwd": s.cwd,
                "pid": s.pid,
                "started_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(s.started_at)
                ),
                "uptime_seconds": int(time.time() - s.started_at),
                "status": "exited" if s.exited else "running",
                "output_preview": (
                    s.output_buffer[-200:] if s.output_buffer else ""
                ),
            }
            if s.exited:
                entry["exit_code"] = s.exit_code
            if s.detached:
                entry["detached"] = True
            result.append(entry)
        return result

    # ----- Session/Task Queries -----

    def has_active_processes(self, task_id: str) -> bool:
        """Check if there are active (running) processes for a task_id."""
        with self._lock:
            sessions = list(self._running.values())

        for session in sessions:
            self._refresh_detached_session(session)

        with self._lock:
            return any(
                s.task_id == task_id and not s.exited
                for s in self._running.values()
            )

    def has_active_for_session(self, session_key: str) -> bool:
        """Check if there are active processes for a session key."""
        with self._lock:
            sessions = list(self._running.values())

        for session in sessions:
            self._refresh_detached_session(session)

        with self._lock:
            return any(
                s.session_key == session_key and not s.exited
                for s in self._running.values()
            )

    def kill_all(self, task_id: str | None = None) -> int:
        """Kill all running processes, optionally filtered by task_id.

        Returns the number of processes killed.
        """
        with self._lock:
            targets = [
                s
                for s in self._running.values()
                if (task_id is None or s.task_id == task_id) and not s.exited
            ]

        killed = 0
        for session in targets:
            result = self.kill_process(session.id)
            if result.get("status") in ("killed", "already_exited"):
                killed += 1
        return killed

    # ----- Cleanup / Pruning -----

    def _prune_if_needed(self) -> None:
        """Remove oldest finished sessions if over MAX_PROCESSES.

        Must hold ``_lock``.
        """
        # First prune expired finished sessions
        now = time.time()
        expired = [
            sid
            for sid, s in self._finished.items()
            if (now - s.started_at) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]

        # If still over limit, remove oldest finished
        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest_id = min(
                self._finished,
                key=lambda sid: self._finished[sid].started_at,
            )
            del self._finished[oldest_id]

    # ----- Checkpoint (crash recovery) -----

    def _write_checkpoint(self) -> None:
        """Write running process metadata to checkpoint file atomically."""
        try:
            with self._lock:
                entries = []
                for s in self._running.values():
                    if not s.exited:
                        entries.append(
                            {
                                "session_id": s.id,
                                "command": s.command,
                                "pid": s.pid,
                                "pid_scope": s.pid_scope,
                                "cwd": s.cwd,
                                "started_at": s.started_at,
                                "task_id": s.task_id,
                                "session_key": s.session_key,
                                "watcher_platform": s.watcher_platform,
                                "watcher_chat_id": s.watcher_chat_id,
                                "watcher_thread_id": s.watcher_thread_id,
                                "watcher_interval": s.watcher_interval,
                                "notify_on_complete": s.notify_on_complete,
                            }
                        )

            _atomic_json_write(CHECKPOINT_PATH, entries)
        except Exception as e:
            logger.debug(
                "Failed to write checkpoint file: %s", e, exc_info=True
            )

    def recover_from_checkpoint(self) -> int:
        """On startup, probe PIDs from checkpoint file.

        Returns the number of processes recovered as detached.
        """
        if not CHECKPOINT_PATH.exists():
            return 0

        try:
            entries = json.loads(
                CHECKPOINT_PATH.read_text(encoding="utf-8")
            )
        except Exception:
            return 0

        recovered = 0
        for entry in entries:
            pid = entry.get("pid")
            if not pid:
                continue

            pid_scope = entry.get("pid_scope", "host")
            if pid_scope != "host":
                logger.info(
                    "Skipping recovery for non-host process: %s "
                    "(pid=%s, scope=%s)",
                    entry.get("command", "unknown")[:60],
                    pid,
                    pid_scope,
                )
                continue

            # Check if PID is still alive
            alive = self._is_host_pid_alive(pid)

            if alive:
                session = ProcessSession(
                    id=entry["session_id"],
                    command=entry.get("command", "unknown"),
                    task_id=entry.get("task_id", ""),
                    session_key=entry.get("session_key", ""),
                    pid=pid,
                    pid_scope=pid_scope,
                    cwd=entry.get("cwd"),
                    started_at=entry.get("started_at", time.time()),
                    detached=True,
                    watcher_platform=entry.get("watcher_platform", ""),
                    watcher_chat_id=entry.get("watcher_chat_id", ""),
                    watcher_thread_id=entry.get("watcher_thread_id", ""),
                    watcher_interval=entry.get("watcher_interval", 0),
                    notify_on_complete=entry.get(
                        "notify_on_complete", False
                    ),
                )
                with self._lock:
                    self._running[session.id] = session
                recovered += 1
                logger.info(
                    "Recovered detached process: %s (pid=%d)",
                    session.command[:60],
                    pid,
                )

                # Re-enqueue watcher so harness can resume notifications
                if session.watcher_interval > 0:
                    self.pending_watchers.append(
                        {
                            "session_id": session.id,
                            "check_interval": session.watcher_interval,
                            "session_key": session.session_key,
                            "platform": session.watcher_platform,
                            "chat_id": session.watcher_chat_id,
                            "thread_id": session.watcher_thread_id,
                            "notify_on_complete": session.notify_on_complete,
                        }
                    )

        self._write_checkpoint()

        return recovered


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

process_registry = ProcessRegistry()


# ---------------------------------------------------------------------------
# "process" tool schema (exposed to the LLM)
# ---------------------------------------------------------------------------

PROCESS_TOOL_DESCRIPTION = (
    "Manage background processes started with terminal(background=true). "
    "Actions: 'list' (show all), 'poll' (check status + new output), "
    "'log' (full output with pagination), 'wait' (block until done or "
    "timeout), 'kill' (terminate), 'write' (send raw stdin data without "
    "newline), 'submit' (send data + Enter, for answering prompts)."
)

PROCESS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "list",
                "poll",
                "log",
                "wait",
                "kill",
                "write",
                "submit",
            ],
            "description": "Action to perform on background processes",
        },
        "session_id": {
            "type": "string",
            "description": (
                "Process session ID (from terminal background output). "
                "Required for all actions except 'list'."
            ),
        },
        "data": {
            "type": "string",
            "description": (
                "Text to send to process stdin "
                "(for 'write' and 'submit' actions)"
            ),
        },
        "timeout": {
            "type": "integer",
            "description": (
                "Max seconds to block for 'wait' action. "
                "Returns partial output on timeout."
            ),
            "minimum": 1,
        },
        "offset": {
            "type": "integer",
            "description": (
                "Line offset for 'log' action (default: last 200 lines)"
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Max lines to return for 'log' action",
            "minimum": 1,
        },
    },
    "required": ["action"],
}


# ---------------------------------------------------------------------------
# Process tool handler
# ---------------------------------------------------------------------------

def _handle_process(args: dict[str, Any], **kw: Any) -> str:
    """Handle ``process`` tool invocations.

    Dispatches to the appropriate :class:`ProcessRegistry` method based on
    the ``action`` field in *args*.
    """
    task_id = kw.get("task_id")
    action = args.get("action", "")
    # Coerce to string -- some models send session_id as an integer
    session_id = (
        str(args.get("session_id", ""))
        if args.get("session_id") is not None
        else ""
    )

    if action == "list":
        return json.dumps(
            {"processes": process_registry.list_sessions(task_id=task_id)},
            ensure_ascii=False,
        )
    elif action in ("poll", "log", "wait", "kill", "write", "submit"):
        if not session_id:
            return json.dumps(
                {
                    "status": "error",
                    "error": f"session_id is required for {action}",
                },
                ensure_ascii=False,
            )
        if action == "poll":
            return json.dumps(
                process_registry.poll(session_id), ensure_ascii=False
            )
        elif action == "log":
            return json.dumps(
                process_registry.read_log(
                    session_id,
                    offset=args.get("offset", 0),
                    limit=args.get("limit", 200),
                ),
                ensure_ascii=False,
            )
        elif action == "wait":
            return json.dumps(
                process_registry.wait(
                    session_id, timeout=args.get("timeout")
                ),
                ensure_ascii=False,
            )
        elif action == "kill":
            return json.dumps(
                process_registry.kill_process(session_id),
                ensure_ascii=False,
            )
        elif action == "write":
            return json.dumps(
                process_registry.write_stdin(
                    session_id, str(args.get("data", ""))
                ),
                ensure_ascii=False,
            )
        elif action == "submit":
            return json.dumps(
                process_registry.submit_stdin(
                    session_id, str(args.get("data", ""))
                ),
                ensure_ascii=False,
            )
    return json.dumps(
        {
            "status": "error",
            "error": (
                f"Unknown process action: {action}. "
                "Use: list, poll, log, wait, kill, write, submit"
            ),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register(registry: Any) -> None:
    """Register the ``process`` tool with the given tool registry.

    Imports :class:`~surogates.tools.registry.ToolSchema` locally to avoid
    circular imports at module level.
    """
    from surogates.tools.registry import ToolSchema

    if registry.has("process"):
        return

    registry.register(
        name="process",
        schema=ToolSchema(
            name="process",
            description=PROCESS_TOOL_DESCRIPTION,
            parameters=PROCESS_SCHEMA,
        ),
        handler=_handle_process,
        toolset="terminal",
        is_async=False,
    )
