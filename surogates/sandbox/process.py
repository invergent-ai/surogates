"""Subprocess-based sandbox backend.

Provides command execution inside ephemeral temporary directories with
restricted environment variables and configurable timeouts.  Suitable for
single-node development and CI; the Kubernetes backend replaces this in
production multi-tenant deployments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from surogates.sandbox.base import SandboxSpec, SandboxStatus
from surogates.tools.utils.checkpoint_manager import CheckpointManager

logger = logging.getLogger(__name__)

# Hard cap on captured stdout + stderr to prevent memory exhaustion.
_MAX_OUTPUT_BYTES: int = 1_048_576  # 1 MiB


@dataclass
class _SandboxEntry:
    """Internal bookkeeping for a single provisioned sandbox."""

    sandbox_id: str
    workdir: Path
    spec: SandboxSpec
    process: asyncio.subprocess.Process | None = None
    env: dict[str, str] = field(default_factory=dict)
    checkpoint_mgr: Any = None  # Lazy-init CheckpointManager


class ProcessSandbox:
    """Sandbox backend that executes commands via ``asyncio.create_subprocess_exec``."""

    def __init__(self) -> None:
        self._sandboxes: dict[str, _SandboxEntry] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Sandbox protocol
    # ------------------------------------------------------------------

    async def provision(self, spec: SandboxSpec) -> str:
        """Create a temporary directory as the sandbox workspace.

        Returns a UUID sandbox identifier used in all subsequent calls.
        """
        sandbox_id = uuid.uuid4().hex
        workdir = Path(tempfile.mkdtemp(prefix=f"sbx-{sandbox_id[:8]}-"))

        # Build a restricted environment: only essential variables plus any
        # caller-provided overrides from the spec.
        restricted_env: dict[str, str] = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(workdir),
            "TERM": "dumb",
        }
        restricted_env.update(spec.env)

        entry = _SandboxEntry(
            sandbox_id=sandbox_id,
            workdir=workdir,
            spec=spec,
            env=restricted_env,
        )

        async with self._lock:
            self._sandboxes[sandbox_id] = entry

        logger.info("Provisioned process sandbox %s at %s", sandbox_id, workdir)
        return sandbox_id

    async def execute(self, sandbox_id: str, name: str, input: str) -> str:
        """Run *name* as a subprocess inside the sandbox directory.

        Internal commands (prefixed with ``_``) are handled in-process
        without spawning a subprocess.  Currently supported:

        - ``_checkpoint`` — filesystem checkpoint via shadow git repo.

        Parameters
        ----------
        sandbox_id:
            Identifier returned by :meth:`provision`.
        name:
            The command (executable name or path) to run.
        input:
            Passed to the process on *stdin*.

        Returns
        -------
        str
            A JSON-encoded object with keys ``exit_code``, ``stdout``,
            ``stderr``, ``truncated``, and ``timed_out``.
        """
        entry = self._get_entry(sandbox_id)

        # Internal commands — handled in-process, not as subprocesses.
        if name == "_checkpoint":
            return await self._handle_checkpoint(entry, input)

        try:
            proc = await asyncio.create_subprocess_exec(
                name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(entry.workdir),
                env=entry.env,
            )
        except FileNotFoundError:
            return self._result_json(
                exit_code=-1,
                stdout="",
                stderr=f"command not found: {name}",
                truncated=False,
                timed_out=False,
            )
        except OSError as exc:
            return self._result_json(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                truncated=False,
                timed_out=False,
            )

        entry.process = proc

        timed_out = False
        try:
            raw_stdout, raw_stderr = await asyncio.wait_for(
                proc.communicate(input=input.encode()),
                timeout=entry.spec.timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            raw_stdout, raw_stderr = await proc.communicate()
        finally:
            entry.process = None

        truncated = False
        if len(raw_stdout) > _MAX_OUTPUT_BYTES:
            raw_stdout = raw_stdout[:_MAX_OUTPUT_BYTES]
            truncated = True
        if len(raw_stderr) > _MAX_OUTPUT_BYTES:
            raw_stderr = raw_stderr[:_MAX_OUTPUT_BYTES]
            truncated = True

        return self._result_json(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=raw_stdout.decode(errors="replace"),
            stderr=raw_stderr.decode(errors="replace"),
            truncated=truncated,
            timed_out=timed_out,
        )

    async def _handle_checkpoint(self, entry: _SandboxEntry, input: str) -> str:
        """Handle internal ``_checkpoint`` commands.

        Supported actions:
        - ``take`` — snapshot the workspace (deduped per turn).
        - ``new_turn`` — reset per-turn dedup.
        - ``latest_hash`` — return the latest checkpoint hash.
        - ``list`` — list available checkpoints.
        - ``restore`` — restore to a checkpoint.
        """
        import asyncio as _asyncio

        try:
            args = json.loads(input) if input else {}
        except json.JSONDecodeError:
            args = {}

        action = args.get("action", "take")
        workdir = str(entry.workdir)

        # Lazy-init the checkpoint manager for this sandbox.
        if entry.checkpoint_mgr is None:
            entry.checkpoint_mgr = CheckpointManager(enabled=True)

        mgr = entry.checkpoint_mgr

        if action == "new_turn":
            mgr.new_turn()
            return json.dumps({"success": True, "action": "new_turn"})

        if action == "take":
            reason = args.get("reason", "auto")
            file_path = args.get("file_path")
            if file_path:
                workdir = mgr.get_working_dir_for_path(file_path)
            ok = await _asyncio.to_thread(mgr.ensure_checkpoint, workdir, reason)
            result: dict[str, Any] = {"success": ok, "action": "take"}
            if ok:
                hash_val = await _asyncio.to_thread(mgr.latest_hash, workdir)
                if hash_val:
                    result["hash"] = hash_val
            return json.dumps(result)

        if action == "latest_hash":
            hash_val = await _asyncio.to_thread(mgr.latest_hash, workdir)
            return json.dumps({"success": True, "hash": hash_val})

        if action == "list":
            checkpoints = await _asyncio.to_thread(mgr.list_checkpoints, workdir)
            return json.dumps({"success": True, "checkpoints": checkpoints})

        if action == "restore":
            commit_hash = args.get("hash", "")
            file_path = args.get("file_path")
            result = await _asyncio.to_thread(mgr.restore, workdir, commit_hash, file_path)
            return json.dumps(result)

        return json.dumps({"success": False, "error": f"Unknown checkpoint action: {action}"})

    async def destroy(self, sandbox_id: str) -> None:
        """Kill any running process and remove the sandbox workspace."""
        async with self._lock:
            entry = self._sandboxes.pop(sandbox_id, None)

        if entry is None:
            logger.warning("Attempted to destroy unknown sandbox %s", sandbox_id)
            return

        # Kill a still-running process.
        if entry.process is not None:
            try:
                entry.process.kill()
                await entry.process.wait()
            except ProcessLookupError:
                pass

        # Remove the temporary directory tree.
        try:
            shutil.rmtree(entry.workdir, ignore_errors=True)
        except Exception:
            logger.exception("Failed to remove workdir %s", entry.workdir)

        logger.info("Destroyed process sandbox %s", sandbox_id)

    async def status(self, sandbox_id: str) -> SandboxStatus:
        """Return ``RUNNING`` if the sandbox exists in the tracking dict."""
        async with self._lock:
            if sandbox_id in self._sandboxes:
                return SandboxStatus.RUNNING
        return SandboxStatus.TERMINATED

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_entry(self, sandbox_id: str) -> _SandboxEntry:
        try:
            return self._sandboxes[sandbox_id]
        except KeyError:
            raise ValueError(f"Unknown sandbox: {sandbox_id}") from None

    @staticmethod
    def _result_json(
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        truncated: bool,
        timed_out: bool,
    ) -> str:
        payload: dict[str, Any] = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
            "timed_out": timed_out,
        }
        return json.dumps(payload)
