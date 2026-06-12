# Persistent Sandbox Tool-Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-call K8s-exec'd `tool-executor` script with a persistent in-pod daemon reached over pod-IP HTTP, and stop serializing sandbox tool calls in `SandboxPool` — taking warm filesystem tools from ~5–35 s to sub-second.

**Architecture:** A FastAPI daemon (`surogates/sandbox/executor_server.py`) becomes the sandbox container's main process. It imports the tool registry once at boot, then forks a child per `/execute` request (copy-on-write — handlers keep today's process-per-call semantics: blocking I/O, CPU burn, and crashes never touch the serving loop). The worker (`K8sSandbox.execute`) POSTs to `http://<pod-ip>:8071/execute` with a per-sandbox bearer token; the K8s-exec machinery is deleted. A mount-gated `/healthz` readinessProbe makes pod-Ready mean "registry warm + geesefs mounted". `SandboxPool.execute` stops holding the session lock during execution.

**Tech Stack:** Python 3.12, FastAPI + uvicorn (already core deps), `multiprocessing` fork context, aiohttp client (already present via kubernetes-asyncio), pytest with `asyncio_mode=auto`.

**Spec:** `docs/superpowers/specs/2026-06-12-persistent-sandbox-executor-design.md`

**Repo:** `/work/surogates` (branch `persistent-tool-executor` off `master`)

**Test command:** `uv run pytest tests/<file> -v` (pytest config in pyproject.toml; `asyncio_mode=auto`, so `async def` tests need no decorator)

## Status

- [x] Task 1: Branch + SandboxPool lock fix
- [x] Task 2: executor_server module — mount detection
- [x] Task 3: run_tool — the child-side dispatch
- [x] Task 4: execute_in_child — the fork runner
- [x] Task 5: create_app — HTTP layer
- [x] Task 6: main() entry, thin-client CLI, Dockerfile CMD
- [x] Task 7: Worker-side settings, pod manifest, provisioning
- [x] Task 8: execute() over HTTP; delete the exec machinery
- [x] Task 9: NetworkPolicy manifest
- [ ] Task 10: Docs, integration test, full verification

---

### Task 1: Branch + SandboxPool lock fix

The pool currently holds the per-session lock across the entire backend call
(`surogates/sandbox/pool.py:108-120`), serializing every parallel tool batch.
The harness already gates which tools may run concurrently
(`should_parallelize` in `surogates/harness/tool_exec.py`), so the pool only
needs the lock to read the mapping.

**Files:**
- Modify: `surogates/sandbox/pool.py:108-120`
- Test: `tests/test_sandbox.py` (append to `TestSandboxPool`)

- [x] **Step 1: Create the branch**

```bash
cd /work/surogates && git checkout -b persistent-tool-executor
```

- [x] **Step 2: Write the failing test**

Append to the `TestSandboxPool` class in `tests/test_sandbox.py`:

```python
    async def test_execute_calls_overlap_for_same_session(self):
        """Two concurrent execute() calls must not serialize on the session lock."""
        import time

        class SlowBackend:
            async def provision(self, spec):
                return "sb-1"

            async def status(self, sandbox_id):
                from surogates.sandbox.base import SandboxStatus
                return SandboxStatus.RUNNING

            async def execute(self, sandbox_id, name, input):
                await asyncio.sleep(0.3)
                return "{}"

            async def destroy(self, sandbox_id):
                pass

        pool = SandboxPool(SlowBackend())
        await pool.ensure("session-1", SandboxSpec())

        start = time.monotonic()
        await asyncio.gather(
            pool.execute("session-1", "tool_a", "{}"),
            pool.execute("session-1", "tool_b", "{}"),
        )
        elapsed = time.monotonic() - start
        # Serialized: ~0.6s. Concurrent: ~0.3s.
        assert elapsed < 0.5, f"execute() calls serialized: {elapsed:.2f}s"
```

Check the imports at the top of `tests/test_sandbox.py` — `asyncio`,
`SandboxPool`, and `SandboxSpec` are already imported there.

- [x] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_sandbox.py::TestSandboxPool::test_execute_calls_overlap_for_same_session -v`
Expected: FAIL with `execute() calls serialized: 0.60s`

- [x] **Step 4: Implement the lock fix**

In `surogates/sandbox/pool.py`, replace the `execute` method (lines 108-120):

```python
    async def execute(self, session_id: str, name: str, input: str) -> str:
        """Execute a command in the sandbox belonging to *session_id*.

        The session lock is held only while resolving the sandbox id —
        not across the backend call.  The harness already decides which
        tool batches may run concurrently (``should_parallelize``), so
        holding the lock here would serialize them for no benefit.  A
        ``destroy_for_session`` racing an in-flight call makes that call
        fail exactly like a pod dying mid-execution, which the caller
        already handles.

        Raises :class:`ValueError` if the session has no associated sandbox.
        """
        lock = await self._session_lock(session_id)
        async with lock:
            sandbox_id = self._mapping.get(session_id)
        if sandbox_id is None:
            raise ValueError(
                f"No sandbox provisioned for session {session_id}"
            )
        return await self._backend.execute(sandbox_id, name, input)
```

- [x] **Step 5: Run the test file to verify it passes (and nothing broke)**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: all PASS, including `test_execute_calls_overlap_for_same_session`

- [x] **Step 6: Commit**

```bash
git add surogates/sandbox/pool.py tests/test_sandbox.py
git commit -m "Stop holding the session lock across sandbox tool execution"
```

---

### Task 2: executor_server module — mount detection

Create the daemon module with its docstring, constants, and the
`workspace_mounted` helper. `os.path.ismount` is wrong here (the bare
emptyDir bind-mount makes `/workspace` a mount point before geesefs lands),
so parse `/proc/mounts` for a `fuse*` entry.

**Files:**
- Create: `surogates/sandbox/executor_server.py`
- Create: `tests/test_executor_server.py`

- [x] **Step 1: Write the failing tests**

Create `tests/test_executor_server.py`:

```python
"""Tests for surogates.sandbox.executor_server — the persistent in-pod daemon."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from surogates.sandbox import executor_server


# ---------------------------------------------------------------------------
# workspace_mounted
# ---------------------------------------------------------------------------


class TestWorkspaceMounted:
    def test_fuse_mount_detected(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text(
            "overlay / overlay rw 0 0\n"
            "geesefs /workspace fuse.geesefs rw,nosuid,nodev 0 0\n"
        )
        assert executor_server.workspace_mounted("/workspace", str(mounts)) is True

    def test_plain_bind_mount_is_not_enough(self, tmp_path):
        # The emptyDir volumeMount makes /workspace a mount point with a
        # non-FUSE fstype — that must NOT count as "geesefs is up".
        mounts = tmp_path / "mounts"
        mounts.write_text(
            "overlay / overlay rw 0 0\n"
            "/dev/sda1 /workspace ext4 rw 0 0\n"
        )
        assert executor_server.workspace_mounted("/workspace", str(mounts)) is False

    def test_no_entry(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text("overlay / overlay rw 0 0\n")
        assert executor_server.workspace_mounted("/workspace", str(mounts)) is False

    def test_trailing_slash_normalized(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text("geesefs /workspace fuse.geesefs rw 0 0\n")
        assert executor_server.workspace_mounted("/workspace/", str(mounts)) is True

    def test_unreadable_mounts_file(self):
        assert executor_server.workspace_mounted("/workspace", "/nonexistent") is False
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_executor_server.py -v`
Expected: FAIL with `ImportError: cannot import name 'executor_server'` (module doesn't exist)

- [x] **Step 3: Create the module**

Create `surogates/sandbox/executor_server.py`. Note: several imports
(`hmac`, `os`, `sys`, FastAPI symbols) are used by functions added in the
following tasks — if a linter complains about unused imports at this
commit, move each import into the task that first uses it.

```python
"""Persistent tool-executor daemon — runs inside the sandbox pod.

Replaces the per-call K8s-exec'd ``tool-executor`` script.  Boot order:
load the tool registry once (the ~7.5 CPU-second import cost is paid
during pod startup, before the port binds), then serve HTTP:

    POST /execute   {"name": ..., "args": {...}, "timeout": 300}
    GET  /healthz   -> 200 when $WORKSPACE_DIR has a live FUSE mount

Each ``/execute`` forks a child process (``multiprocessing`` fork
context — the warm registry is inherited copy-on-write) that runs the
dispatch on a fresh event loop and writes the result JSON to a pipe.
Tool handlers were written for process-per-call execution: they block,
burn CPU, and can crash — none of which may touch the serving loop, or
the readinessProbe would flap, ``_map_pod_status`` would report
``PENDING``, and ``SandboxPool.ensure`` would destroy the sandbox
mid-tool.  On timeout the child is killed (the old exec path abandoned
the remote process and left it running).

The worker authenticates with ``Authorization: Bearer
$TOOL_EXECUTOR_TOKEN``; ``/healthz`` is unauthenticated (the kubelet
probes it).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import multiprocessing
import os
import sys
from typing import Any

from fastapi import FastAPI, Request, Response

logger = logging.getLogger("tool-executor")

DEFAULT_PORT = 8071
DEFAULT_TIMEOUT = 300
MAX_CONCURRENCY = 8

# Fork context: children inherit the warm registry copy-on-write.  The
# spawn context would re-import everything and defeat the daemon.
_MP = multiprocessing.get_context("fork")

# Populated once by init_registry() before the port binds; forked
# children read it via module global.
_REGISTRY: Any = None


def init_registry() -> Any:
    """Import and build the tool registry (the expensive part, run once)."""
    global _REGISTRY
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.runtime import ToolRuntime

    registry = ToolRegistry()
    runtime = ToolRuntime(registry)
    runtime.register_builtins()
    _REGISTRY = registry
    return registry


def workspace_mounted(workspace: str, mounts_path: str = "/proc/mounts") -> bool:
    """Return ``True`` when *workspace* has a live FUSE mount.

    ``os.path.ismount`` is not usable here: the emptyDir volumeMount
    already makes the workspace a mount point before the geesefs
    sidecar's FUSE mount propagates in, so the fstype must be checked.
    The ``.s3fs-mounted`` sentinel is fleet-mode-only and never written
    by the legacy entrypoint sandbox pods run.
    """
    target = workspace.rstrip("/") or "/"
    try:
        with open(mounts_path, encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                if (
                    len(fields) >= 3
                    and fields[1] == target
                    and fields[2].startswith("fuse")
                ):
                    return True
    except OSError:
        return False
    return False
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_executor_server.py -v`
Expected: 5 PASS

- [x] **Step 5: Commit**

```bash
git add surogates/sandbox/executor_server.py tests/test_executor_server.py
git commit -m "Add executor daemon module with FUSE mount detection"
```

---

### Task 3: run_tool — the child-side dispatch

Port the CLI's dispatch logic verbatim: `_checkpoint` and `_code` branches,
registry dispatch, `Unknown tool` / exception result shapes. This function
runs inside the forked child (it may block freely).

**Files:**
- Modify: `surogates/sandbox/executor_server.py` (append)
- Test: `tests/test_executor_server.py` (append)

- [x] **Step 1: Write the failing tests**

Append to `tests/test_executor_server.py`:

```python
# ---------------------------------------------------------------------------
# run_tool — child-side dispatch
# ---------------------------------------------------------------------------


class FakeRegistry:
    """Stands in for ToolRegistry; behaviors keyed by tool name."""

    async def dispatch(self, name, args, **kwargs):
        if name == "missing":
            raise KeyError(name)
        if name == "boom":
            raise RuntimeError("boom")
        if name == "slow":
            await asyncio.sleep(float(args.get("seconds", 1.0)))
            return json.dumps({"ok": True, "slept": True})
        if name == "spin":  # CPU-bound, never yields
            deadline = time.monotonic() + float(args.get("seconds", 1.0))
            while time.monotonic() < deadline:
                pass
            return json.dumps({"ok": True, "spun": True})
        if name == "die":  # simulates a native-code crash
            os._exit(7)
        return json.dumps({"ok": True, "echo": args, "kwargs_has_workspace": "workspace_path" in kwargs})


@pytest.fixture()
def fake_registry(monkeypatch):
    registry = FakeRegistry()
    monkeypatch.setattr(executor_server, "_REGISTRY", registry)
    return registry


class TestRunTool:
    def test_dispatches_through_registry(self, fake_registry):
        result = json.loads(executor_server.run_tool("echo", {"a": 1}, "/ws"))
        assert result["ok"] is True
        assert result["echo"] == {"a": 1}
        assert result["kwargs_has_workspace"] is True

    def test_unknown_tool(self, fake_registry):
        result = json.loads(executor_server.run_tool("missing", {}, "/ws"))
        assert result == {"exit_code": 1, "output": "", "error": "Unknown tool: missing"}

    def test_handler_exception(self, fake_registry):
        result = json.loads(executor_server.run_tool("boom", {}, "/ws"))
        assert result == {"exit_code": 1, "output": "", "error": "boom"}

    def test_checkpoint_branch(self, monkeypatch):
        class FakeMgr:
            def __init__(self, enabled):
                pass

            def latest_hash(self, workspace):
                return "abc123"

        monkeypatch.setattr(
            "surogates.tools.utils.checkpoint_manager.CheckpointManager", FakeMgr,
        )
        result = json.loads(
            executor_server.run_tool("_checkpoint", {"action": "latest_hash"}, "/ws"),
        )
        assert result == {"success": True, "hash": "abc123"}

    def test_code_branch(self, monkeypatch):
        monkeypatch.setattr(
            "surogates.coding_agents.pod_runner.dispatch",
            lambda args: {"ok": True, "action": args.get("action")},
        )
        result = json.loads(
            executor_server.run_tool("_code", {"action": "status"}, "/ws"),
        )
        assert result == {"ok": True, "action": "status"}
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_executor_server.py::TestRunTool -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'run_tool'`

- [x] **Step 3: Implement run_tool**

Append to `surogates/sandbox/executor_server.py`:

```python
def _run_checkpoint(args: dict, workspace: str) -> str:
    """Handle ``_checkpoint`` internal commands (ported from the old CLI)."""
    from surogates.tools.utils.checkpoint_manager import CheckpointManager

    action = args.get("action", "take")
    mgr = CheckpointManager(enabled=True)
    logger.info("checkpoint action=%s", action)

    if action == "new_turn":
        mgr.new_turn()
        return json.dumps({"success": True, "action": "new_turn"})
    if action == "take":
        reason = args.get("reason", "auto")
        file_path = args.get("file_path")
        workdir = workspace
        if file_path:
            workdir = mgr.get_working_dir_for_path(file_path)
        ok = mgr.ensure_checkpoint(workdir, reason)
        result: dict = {"success": ok, "action": "take"}
        if ok:
            h = mgr.latest_hash(workdir)
            if h:
                result["hash"] = h
        logger.info("checkpoint take: %s", "ok" if ok else "skipped")
        return json.dumps(result)
    if action == "latest_hash":
        return json.dumps({"success": True, "hash": mgr.latest_hash(workspace)})
    if action == "list":
        return json.dumps({"success": True, "checkpoints": mgr.list_checkpoints(workspace)})
    if action == "restore":
        return json.dumps(
            mgr.restore(workspace, args.get("hash", ""), args.get("file_path")),
        )
    return json.dumps({"success": False, "error": f"Unknown checkpoint action: {action}"})


def run_tool(name: str, args: dict, workspace: str) -> str:
    """Dispatch one tool call through the real handlers.

    Runs inside the forked child — blocking calls and CPU burn are fine
    here.  Result shapes mirror the old CLI exactly.
    """
    if name == "_checkpoint":
        return _run_checkpoint(args, workspace)
    if name == "_code":
        # The payload may carry a credential on launch; never log args.
        from surogates.coding_agents.pod_runner import dispatch as code_dispatch

        return json.dumps(code_dispatch(args))

    async def _dispatch() -> str:
        return await _REGISTRY.dispatch(
            name, args, workspace_path=workspace, tools=_REGISTRY,
        )

    try:
        return asyncio.run(_dispatch())
    except KeyError:
        return json.dumps({
            "exit_code": 1,
            "output": "",
            "error": f"Unknown tool: {name}",
        })
    except Exception as exc:
        logger.error("Tool %s raised: %s", name, exc, exc_info=True)
        return json.dumps({
            "exit_code": 1,
            "output": "",
            "error": str(exc),
        })
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_executor_server.py -v`
Expected: all PASS (10 tests)

- [x] **Step 5: Commit**

```bash
git add surogates/sandbox/executor_server.py tests/test_executor_server.py
git commit -m "Port CLI tool dispatch into the executor daemon child path"
```

---

### Task 4: execute_in_child — the fork runner

Fork a child per call, await the result over a pipe, kill on timeout,
classify abnormal child death.

**Files:**
- Modify: `surogates/sandbox/executor_server.py` (append)
- Test: `tests/test_executor_server.py` (append)

- [x] **Step 1: Write the failing tests**

Append to `tests/test_executor_server.py`:

```python
# ---------------------------------------------------------------------------
# execute_in_child — fork runner
# ---------------------------------------------------------------------------


class TestExecuteInChild:
    async def test_result_roundtrip(self, fake_registry):
        result = json.loads(
            await executor_server.execute_in_child("echo", {"x": 2}, "/ws", timeout=10),
        )
        assert result["ok"] is True
        assert result["echo"] == {"x": 2}

    async def test_timeout_kills_child(self, fake_registry):
        start = time.monotonic()
        result = json.loads(
            await executor_server.execute_in_child(
                "slow", {"seconds": 10}, "/ws", timeout=0.3,
            ),
        )
        elapsed = time.monotonic() - start
        assert result["timed_out"] is True
        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"].lower()
        assert elapsed < 5, f"timeout did not kill promptly: {elapsed:.1f}s"

    async def test_child_abnormal_death(self, fake_registry):
        result = json.loads(
            await executor_server.execute_in_child("die", {}, "/ws", timeout=10),
        )
        assert result["exit_code"] == 1
        assert "died" in result["error"]
        assert "7" in result["error"]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_executor_server.py::TestExecuteInChild -v`
Expected: FAIL with `AttributeError: ... has no attribute 'execute_in_child'`

- [x] **Step 3: Implement the fork runner**

Append to `surogates/sandbox/executor_server.py`:

```python
def _timed_out_result() -> str:
    """Transport-level timeout result — mirrors K8sSandbox._result_json."""
    return json.dumps({
        "exit_code": -1,
        "stdout": "",
        "stderr": "Execution timed out",
        "truncated": False,
        "timed_out": True,
    })


def _child_main(conn: Any, name: str, args: dict, workspace: str) -> None:
    """Entry point of the forked child: run the tool, ship the result."""
    # The forked thread inherits the parent's "running event loop"
    # marker; clear it so run_tool's asyncio.run() can start a fresh
    # loop.  Python 3.12's asyncio also resets this in an at-fork hook —
    # this line is belt-and-braces against that hook changing.
    asyncio._set_running_loop(None)
    try:
        result = run_tool(name, args, workspace)
    except BaseException as exc:  # never die without reporting
        result = json.dumps({"exit_code": 1, "output": "", "error": str(exc)})
    try:
        conn.send_bytes(result.encode("utf-8"))
    finally:
        conn.close()


async def execute_in_child(
    name: str, args: dict, workspace: str, timeout: float,
) -> str:
    """Fork a child, run *name* in it, and return its result JSON.

    On timeout the child is SIGKILLed and the standard ``timed_out``
    result is returned — unlike the old exec transport, no orphaned
    process keeps running.  A child that dies without reporting
    (segfault, ``os._exit``) yields an error result and the daemon
    keeps serving.
    """
    parent_conn, child_conn = _MP.Pipe(duplex=False)
    proc = _MP.Process(
        target=_child_main, args=(child_conn, name, args, workspace), daemon=True,
    )
    proc.start()
    child_conn.close()
    try:
        ready = await asyncio.to_thread(parent_conn.poll, timeout)
        if not ready:
            logger.warning(
                "Tool %s timed out after %.0fs; killing child %s",
                name, timeout, proc.pid,
            )
            proc.kill()
            return _timed_out_result()
        try:
            data = await asyncio.to_thread(parent_conn.recv_bytes)
        except EOFError:
            await asyncio.to_thread(proc.join, 5)
            logger.error(
                "Tool %s child died without a result (exit code %s)",
                name, proc.exitcode,
            )
            return json.dumps({
                "exit_code": 1,
                "output": "",
                "error": f"Tool process died unexpectedly (exit code {proc.exitcode})",
            })
        return data.decode("utf-8")
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.kill()
        await asyncio.to_thread(proc.join, 5)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_executor_server.py -v`
Expected: all PASS (13 tests)

- [x] **Step 5: Commit**

```bash
git add surogates/sandbox/executor_server.py tests/test_executor_server.py
git commit -m "Run each tool call in a killable forked child"
```

---

### Task 5: create_app — HTTP layer with auth, healthz, concurrency

**Files:**
- Modify: `surogates/sandbox/executor_server.py` (append)
- Test: `tests/test_executor_server.py` (append)

- [x] **Step 1: Write the failing tests**

Append to `tests/test_executor_server.py` — move the `import httpx` line up
into the import block at the top of the file (shown inline here for
completeness; mid-file imports trip ruff E402):

```python
# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

import httpx


def _make_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://daemon")


@pytest.fixture()
def mounted_mounts_file(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("geesefs /workspace fuse.geesefs rw 0 0\n")
    return str(mounts)


@pytest.fixture()
def app(fake_registry, mounted_mounts_file):
    return executor_server.create_app(
        token="secret-token",
        workspace="/workspace",
        mounts_path=mounted_mounts_file,
    )


AUTH = {"Authorization": "Bearer secret-token"}


class TestHttpLayer:
    async def test_execute_requires_token(self, app):
        async with _make_client(app) as client:
            resp = await client.post("/execute", json={"name": "echo", "args": {}})
            assert resp.status_code == 401
            resp = await client.post(
                "/execute",
                json={"name": "echo", "args": {}},
                headers={"Authorization": "Bearer wrong"},
            )
            assert resp.status_code == 401

    async def test_execute_happy_path(self, app):
        async with _make_client(app) as client:
            resp = await client.post(
                "/execute",
                json={"name": "echo", "args": {"a": 1}},
                headers=AUTH,
            )
            assert resp.status_code == 200
            body = json.loads(resp.text)
            assert body["ok"] is True
            assert body["echo"] == {"a": 1}

    async def test_execute_missing_name(self, app):
        async with _make_client(app) as client:
            resp = await client.post("/execute", json={"args": {}}, headers=AUTH)
            assert resp.status_code == 200
            assert json.loads(resp.text)["error"] == "No tool name provided"

    async def test_execute_body_timeout(self, app):
        async with _make_client(app) as client:
            start = time.monotonic()
            resp = await client.post(
                "/execute",
                json={"name": "slow", "args": {"seconds": 10}, "timeout": 0.3},
                headers=AUTH,
            )
            assert json.loads(resp.text)["timed_out"] is True
            assert time.monotonic() - start < 5

    async def test_healthz_unauthenticated_when_mounted(self, app):
        async with _make_client(app) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200

    async def test_healthz_503_when_not_mounted(self, fake_registry, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text("/dev/sda1 /workspace ext4 rw 0 0\n")
        app = executor_server.create_app(
            token="secret-token", workspace="/workspace", mounts_path=str(mounts),
        )
        async with _make_client(app) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 503

    async def test_concurrent_executes_overlap(self, app):
        async with _make_client(app) as client:
            start = time.monotonic()
            await asyncio.gather(
                client.post(
                    "/execute",
                    json={"name": "slow", "args": {"seconds": 0.6}},
                    headers=AUTH,
                ),
                client.post(
                    "/execute",
                    json={"name": "slow", "args": {"seconds": 0.6}},
                    headers=AUTH,
                ),
            )
            elapsed = time.monotonic() - start
            assert elapsed < 1.1, f"requests serialized: {elapsed:.2f}s"

    async def test_cpu_bound_tool_does_not_block_healthz(self, app):
        async with _make_client(app) as client:
            spin = asyncio.create_task(
                client.post(
                    "/execute",
                    json={"name": "spin", "args": {"seconds": 1.5}},
                    headers=AUTH,
                ),
            )
            await asyncio.sleep(0.1)
            start = time.monotonic()
            resp = await client.get("/healthz")
            elapsed = time.monotonic() - start
            assert resp.status_code == 200
            assert elapsed < 0.5, f"healthz starved by CPU-bound tool: {elapsed:.2f}s"
            await spin

    async def test_child_death_keeps_daemon_serving(self, app):
        async with _make_client(app) as client:
            resp = await client.post(
                "/execute", json={"name": "die", "args": {}}, headers=AUTH,
            )
            assert "died" in json.loads(resp.text)["error"]
            # Daemon still serves after the child crash.
            resp = await client.post(
                "/execute", json={"name": "echo", "args": {}}, headers=AUTH,
            )
            assert json.loads(resp.text)["ok"] is True
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_executor_server.py::TestHttpLayer -v`
Expected: FAIL with `AttributeError: ... has no attribute 'create_app'`

- [x] **Step 3: Implement the HTTP layer**

Append to `surogates/sandbox/executor_server.py`:

```python
def _token_ok(auth_header: str, token: str) -> bool:
    if not auth_header.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth_header[len("Bearer "):], token)


def create_app(
    *,
    token: str,
    workspace: str,
    mounts_path: str = "/proc/mounts",
    max_concurrency: int = MAX_CONCURRENCY,
    default_timeout: int = DEFAULT_TIMEOUT,
) -> FastAPI:
    """Build the daemon's FastAPI app.

    ``token`` and ``workspace`` are injected (instead of read from env
    inside the handlers) so tests can construct isolated apps.
    """
    app = FastAPI()
    sem = asyncio.Semaphore(max_concurrency)

    @app.get("/healthz")
    async def healthz() -> Response:
        if workspace_mounted(workspace, mounts_path):
            return Response(content="ok", status_code=200)
        return Response(content="workspace not mounted", status_code=503)

    @app.post("/execute")
    async def execute(request: Request) -> Response:
        auth = request.headers.get("authorization", "")
        if not _token_ok(auth, token):
            return Response(content="unauthorized", status_code=401)

        payload = await request.json()
        name = payload.get("name") or ""
        args = payload.get("args") or {}
        timeout = float(payload.get("timeout") or default_timeout)

        if not name:
            return Response(
                content=json.dumps({
                    "exit_code": 1,
                    "output": "",
                    "error": "No tool name provided",
                }),
                media_type="application/json",
            )

        # The _code payload may carry a credential — never log its args.
        if name == "_code":
            logger.info("→ _code")
        else:
            preview = json.dumps(args, default=str)[:200]
            logger.info("→ %s %s", name, preview)

        async with sem:
            result = await execute_in_child(name, args, workspace, timeout)
        logger.info("← %s (%d bytes)", name, len(result))
        return Response(content=result, media_type="application/json")

    return app
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_executor_server.py -v`
Expected: all PASS (22 tests)

- [x] **Step 5: Commit**

```bash
git add surogates/sandbox/executor_server.py tests/test_executor_server.py
git commit -m "Serve tool execution over authenticated pod-local HTTP"
```

---

### Task 6: main() entry, thin-client CLI, Dockerfile CMD

The daemon becomes runnable via `python -m surogates.sandbox.executor_server`.
The old `images/sandbox/tool-executor` CLI becomes a thin client that POSTs
to the daemon — one dispatch path, the `kubectl exec` debugging affordance
kept. The old in-CLI dispatch code is deleted.

**Files:**
- Modify: `surogates/sandbox/executor_server.py` (append)
- Rewrite: `images/sandbox/tool-executor`
- Modify: `images/sandbox/Dockerfile` (CMD only)
- Test: `tests/test_executor_server.py` (append)

- [x] **Step 1: Write the failing test (thin client end-to-end over a real port)**

Append to `tests/test_executor_server.py` — move the `subprocess`, `sys`,
and `uvicorn` imports up into the import block at the top of the file
(shown inline here for completeness):

```python
# ---------------------------------------------------------------------------
# Thin-client CLI against a live server
# ---------------------------------------------------------------------------

import subprocess
import sys

import uvicorn

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_PATH = os.path.join(REPO_ROOT, "images", "sandbox", "tool-executor")


class TestThinClientCli:
    async def test_cli_forwards_to_daemon(self, app):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        server = uvicorn.Server(config)
        serve_task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]

            env = {
                **os.environ,
                "TOOL_EXECUTOR_PORT": str(port),
                "TOOL_EXECUTOR_TOKEN": "secret-token",
            }
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, CLI_PATH, "echo", json.dumps({"b": 2})],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert proc.returncode == 0, proc.stderr
            body = json.loads(proc.stdout)
            assert body["ok"] is True
            assert body["echo"] == {"b": 2}
        finally:
            server.should_exit = True
            await serve_task
```

- [x] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_executor_server.py::TestThinClientCli -v`
Expected: FAIL — the current CLI ignores `TOOL_EXECUTOR_PORT` and tries to
dispatch through the real registry (wrong output or import-time failure).

- [x] **Step 3: Add main() to the daemon**

Append to `surogates/sandbox/executor_server.py`:

```python
def main() -> None:
    """Daemon entry point — sandbox container main process."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    workspace = os.environ.get("WORKSPACE_DIR", "/workspace")
    port = int(os.environ.get("TOOL_EXECUTOR_PORT", str(DEFAULT_PORT)))
    token = os.environ.get("TOOL_EXECUTOR_TOKEN", "")
    if not token:
        logger.error("TOOL_EXECUTOR_TOKEN is required")
        sys.exit(1)

    logger.info("Loading tool registry...")
    init_registry()
    logger.info("Registry loaded; serving on 0.0.0.0:%d", port)

    import uvicorn

    uvicorn.run(
        create_app(token=token, workspace=workspace),
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
```

- [x] **Step 4: Rewrite the CLI as a thin client**

Replace the entire contents of `images/sandbox/tool-executor`:

```python
#!/usr/bin/env python3
"""Thin client for the tool-executor daemon — debugging affordance.

The daemon (``surogates.sandbox.executor_server``, the sandbox
container's main process) is the single dispatch path; this script just
forwards one call to it so the familiar invocation keeps working:

    kubectl exec <sandbox-pod> -c sandbox -- tool-executor <name> <json_args>

Output: the daemon's JSON result on stdout (same shapes as before).
"""

import json
import os
import sys
import urllib.request


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.argv[2] if len(sys.argv) > 2 else "{}"

    if not name:
        print(json.dumps({
            "exit_code": 1,
            "output": "",
            "error": "No tool name provided",
        }))
        return

    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        args = {}

    port = os.environ.get("TOOL_EXECUTOR_PORT", "8071")
    token = os.environ.get("TOOL_EXECUTOR_TOKEN", "")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/execute",
        data=json.dumps({"name": name, "args": args}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        sys.stdout.write(resp.read().decode("utf-8"))


if __name__ == "__main__":
    main()
```

- [x] **Step 5: Update the Dockerfile CMD**

In `images/sandbox/Dockerfile`, replace the final line:

```dockerfile
CMD ["sleep", "infinity"]
```

with:

```dockerfile
CMD ["python", "-m", "surogates.sandbox.executor_server"]
```

(The pod manifest sets an explicit `command` anyway — see Task 7 — but the
image default should match the real role.)

- [x] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_executor_server.py -v`
Expected: all PASS (23 tests)

- [x] **Step 7: Commit**

```bash
git add surogates/sandbox/executor_server.py images/sandbox/tool-executor images/sandbox/Dockerfile tests/test_executor_server.py
git commit -m "Make the executor daemon the sandbox main process; CLI becomes a thin client"
```

---

### Task 7: Worker-side settings, pod manifest, provisioning

Add the port setting, generate a per-sandbox token, run the daemon as the
container command, wire the readinessProbe, capture the pod IP after Ready.
Remove the now-meaningless `executor_path` knob (no legacy fallback).

**Files:**
- Modify: `surogates/config.py` (SandboxSettings, ~line 351)
- Modify: `surogates/sandbox/kubernetes.py` (`__init__`, `_PodEntry`, `provision`, `_build_pod_manifest`)
- Modify: `surogates/orchestrator/worker.py:592` (K8sSandbox construction)
- Test: `tests/test_k8s_sandbox.py`

- [x] **Step 1: Write the failing tests**

In `tests/test_k8s_sandbox.py`, update the `sandbox` fixture (line ~21):
replace `executor_path="/usr/local/bin/tool-executor",` with
`executor_port=8071,`.

`_build_pod_manifest` gains a required keyword-only `executor_token`
parameter in Step 3, so update the four existing callsites (lines 43, 68,
84, 94) to pass it:

```bash
sed -i 's/_build_pod_manifest(\("[^)]*\), spec)/_build_pod_manifest(\1, spec, executor_token="t")/; s/_build_pod_manifest(\("[^)]*\), SandboxSpec())/_build_pod_manifest(\1, SandboxSpec(), executor_token="t")/' tests/test_k8s_sandbox.py
grep -Fn "_build_pod_manifest" tests/test_k8s_sandbox.py   # verify all calls now pass executor_token
```

Then append a new test class:

```python
class TestExecutorWiring:
    """Daemon command, token env, port env, and readinessProbe in the manifest."""

    def test_sandbox_container_runs_daemon(self, sandbox: K8sSandbox):
        spec = SandboxSpec()
        pod = sandbox._build_pod_manifest("id", "pod", "secret", spec, executor_token="tok-123")
        container = pod.spec.containers[0]
        assert container.command == [
            "tini", "--", "python", "-m", "surogates.sandbox.executor_server",
        ]

    def test_executor_env_injected(self, sandbox: K8sSandbox):
        spec = SandboxSpec()
        pod = sandbox._build_pod_manifest("id", "pod", "secret", spec, executor_token="tok-123")
        env = {e.name: e.value for e in pod.spec.containers[0].env}
        assert env["TOOL_EXECUTOR_TOKEN"] == "tok-123"
        assert env["TOOL_EXECUTOR_PORT"] == "8071"

    def test_readiness_probe(self, sandbox: K8sSandbox):
        spec = SandboxSpec()
        pod = sandbox._build_pod_manifest("id", "pod", "secret", spec, executor_token="t")
        probe = pod.spec.containers[0].readiness_probe
        assert probe.http_get.path == "/healthz"
        assert probe.http_get.port == 8071
        assert probe.period_seconds == 1
        assert probe.timeout_seconds == 2
        assert probe.failure_threshold == 15
```

And add a provisioning test that asserts pod IP + token capture (place it
next to the existing provision tests, reusing their mock style — see
`test_success` at line ~195 for the established pattern of mocking
`_get_api`, `_create_s3_secret`, and `_wait_for_ready`):

```python
class TestProvisionCapturesEndpoint:
    async def test_pod_ip_and_token_stored(self, sandbox: K8sSandbox):
        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        pod = MagicMock()
        pod.status.pod_ip = "10.42.0.99"
        api.read_namespaced_pod = AsyncMock(return_value=pod)

        with patch.object(sandbox, "_get_api", AsyncMock(return_value=api)), \
             patch.object(sandbox, "_create_s3_secret", AsyncMock()), \
             patch.object(sandbox, "_wait_for_ready", AsyncMock()):
            sandbox_id = await sandbox.provision(SandboxSpec())

        entry = sandbox._pods[sandbox_id]
        assert entry.pod_ip == "10.42.0.99"
        assert len(entry.token) >= 32

    async def test_missing_pod_ip_fails_provision(self, sandbox: K8sSandbox):
        from surogates.sandbox.base import SandboxUnavailableError

        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        pod = MagicMock()
        pod.status.pod_ip = None
        api.read_namespaced_pod = AsyncMock(return_value=pod)
        api.delete_namespaced_pod = AsyncMock()

        with patch.object(sandbox, "_get_api", AsyncMock(return_value=api)), \
             patch.object(sandbox, "_create_s3_secret", AsyncMock()), \
             patch.object(sandbox, "_delete_secret_safe", AsyncMock()), \
             patch.object(sandbox, "_wait_for_ready", AsyncMock()):
            with pytest.raises(SandboxUnavailableError):
                await sandbox.provision(SandboxSpec())
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_k8s_sandbox.py -v`
Expected: the new tests FAIL (`unexpected keyword argument 'executor_port'`,
`'executor_token'`); pre-existing manifest tests now also FAIL on the changed
fixture — that's expected until Step 3 lands.

- [x] **Step 3: Implement settings + kubernetes.py changes**

**`surogates/config.py`** — in `SandboxSettings`, replace:

```python
    k8s_executor_path: str = "/usr/local/bin/tool-executor"
```

with:

```python
    # Port the in-pod tool-executor daemon listens on (pod-IP HTTP).
    k8s_executor_port: int = 8071
```

**`surogates/sandbox/kubernetes.py`:**

(a) Add to the module imports (top of file): `import secrets` and
`import aiohttp` (aiohttp ships with kubernetes-asyncio and is already
imported function-locally today).

(b) `_PodEntry` — add two fields after `spec`:

```python
    spec: SandboxSpec
    pod_ip: str = ""
    token: str = ""
    status: SandboxStatus = SandboxStatus.PENDING
```

(c) `K8sSandbox.__init__` — replace the `executor_path` parameter and its
docstring entry:

```python
        executor_path: str = "/usr/local/bin/tool-executor",
```

becomes:

```python
        executor_port: int = 8071,
```

and `self._executor_path = executor_path` becomes
`self._executor_port = executor_port`. Update the docstring lines 66-69 from
"Path to the tool-executor binary inside the sandbox image." to
"Port the tool-executor daemon listens on inside the sandbox pod.".
Also add `self._http: aiohttp.ClientSession | None = None` next to
`self._api = None` (used in Task 8).

(d) `provision()` — generate the token and capture the pod IP. Replace the
manifest-build call:

```python
        pod_manifest = self._build_pod_manifest(
            sandbox_id, pod_name, secret_name, spec,
        )
```

with:

```python
        executor_token = secrets.token_urlsafe(32)
        pod_manifest = self._build_pod_manifest(
            sandbox_id, pod_name, secret_name, spec,
            executor_token=executor_token,
        )
```

Add `token=executor_token,` to the `_PodEntry(...)` construction, and extend
the existing ready-wait `try` block:

```python
        try:
            await self._wait_for_ready(api, pod_name)
            entry.status = SandboxStatus.RUNNING
        except Exception as exc:
            ...
```

becomes:

```python
        try:
            await self._wait_for_ready(api, pod_name)
            pod = await api.read_namespaced_pod(pod_name, self._namespace)
            entry.pod_ip = (pod.status.pod_ip if pod.status else "") or ""
            if not entry.pod_ip:
                raise RuntimeError(f"Pod {pod_name} has no IP after becoming ready")
            entry.status = SandboxStatus.RUNNING
        except Exception as exc:
            ...
```

(the `except` body is unchanged — it already destroys the entry and raises
`SandboxUnavailableError`).

(e) `_build_pod_manifest` — add the keyword-only parameter:

```python
    def _build_pod_manifest(
        self,
        sandbox_id: str,
        pod_name: str,
        secret_name: str,
        spec: SandboxSpec,
        *,
        executor_token: str,
    ) -> client.V1Pod:
```

Append to `env_vars` (right after the `WORKSPACE_DIR` entry):

```python
        env_vars.append(client.V1EnvVar(
            name="TOOL_EXECUTOR_PORT", value=str(self._executor_port),
        ))
        env_vars.append(client.V1EnvVar(
            name="TOOL_EXECUTOR_TOKEN", value=executor_token,
        ))
```

In the `sandbox_container = client.V1Container(...)` construction, replace:

```python
            command=["sleep", "infinity"],
```

with:

```python
            # The daemon is the container's main process; its death
            # terminates the container (restartPolicy=Never -> pod Failed
            # -> pool status check reprovisions).
            command=["tini", "--", "python", "-m", "surogates.sandbox.executor_server"],
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path="/healthz", port=self._executor_port,
                ),
                # Fast checks for quick provisioning; the high failure
                # threshold tolerates transient kubelet/CNI blips — the
                # fork-per-request daemon never starves /healthz, so 15
                # consecutive failures means genuinely broken (and the
                # pool's reprovision is then desired self-healing).
                period_seconds=1,
                timeout_seconds=2,
                failure_threshold=15,
            ),
```

**`surogates/orchestrator/worker.py:592`** — replace:

```python
            executor_path=settings.sandbox.k8s_executor_path,
```

with:

```python
            executor_port=settings.sandbox.k8s_executor_port,
```

- [x] **Step 4: Run the test file**

Run: `uv run pytest tests/test_k8s_sandbox.py -v`
Expected: new tests PASS; pre-existing tests PASS again (fixture +
callsites updated in Step 1). If any old test asserts on
`command == ["sleep", "infinity"]`, update that assertion to the new
daemon command.

- [x] **Step 5: Commit**

```bash
git add surogates/config.py surogates/sandbox/kubernetes.py surogates/orchestrator/worker.py tests/test_k8s_sandbox.py
git commit -m "Provision sandbox pods with the executor daemon, token, and readiness probe"
```

---

### Task 8: execute() over HTTP; delete the exec machinery

**Files:**
- Modify: `surogates/sandbox/kubernetes.py` (`execute`, new `_get_http`/`aclose`; delete `_exec_in_pod`)
- Modify: `surogates/orchestrator/worker.py:1297` (shutdown)
- Test: `tests/test_k8s_sandbox.py`

- [x] **Step 1: Write the failing tests**

Append to `tests/test_k8s_sandbox.py` — move the three import lines up into
the import block at the top of the file (shown inline here for
completeness). These tests run a real local aiohttp server so
connection-refused and status-code behavior are genuine:

```python
import aiohttp
from aiohttp import web

from surogates.sandbox.base import SandboxUnavailableError


def _entry_for(sandbox: K8sSandbox, *, port: int, timeout: int = 5) -> _PodEntry:
    entry = _PodEntry(
        sandbox_id="sb-test",
        pod_name="sandbox-test",
        secret_name="secret-test",
        namespace="test-ns",
        spec=SandboxSpec(timeout=timeout),
        pod_ip="127.0.0.1",
        token="tok-abc",
        status=SandboxStatus.RUNNING,
    )
    sandbox._pods["sb-test"] = entry
    sandbox._executor_port = port
    return entry


async def _serve(handler) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_post("/execute", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


class TestExecuteHttp:
    async def test_result_passthrough_and_auth_header(self, sandbox: K8sSandbox):
        seen = {}

        async def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = await request.json()
            return web.Response(text='{"ok": true}', content_type="application/json")

        runner, port = await _serve(handler)
        try:
            _entry_for(sandbox, port=port)
            result = await sandbox.execute("sb-test", "list_files", '{"pattern": "*"}')
            assert json.loads(result) == {"ok": True}
            assert seen["auth"] == "Bearer tok-abc"
            assert seen["body"] == {
                "name": "list_files",
                "args": {"pattern": "*"},
                "timeout": 5,
            }
        finally:
            await runner.cleanup()
            await sandbox.aclose()

    async def test_connect_refused_marks_failed_and_raises(self, sandbox: K8sSandbox):
        entry = _entry_for(sandbox, port=1)  # nothing listens on port 1
        with pytest.raises(SandboxUnavailableError):
            await sandbox.execute("sb-test", "list_files", "{}")
        assert entry.status == SandboxStatus.FAILED
        await sandbox.aclose()

    async def test_401_marks_failed_and_raises(self, sandbox: K8sSandbox):
        async def handler(request):
            return web.Response(status=401, text="unauthorized")

        runner, port = await _serve(handler)
        try:
            entry = _entry_for(sandbox, port=port)
            with pytest.raises(SandboxUnavailableError):
                await sandbox.execute("sb-test", "list_files", "{}")
            assert entry.status == SandboxStatus.FAILED
        finally:
            await runner.cleanup()
            await sandbox.aclose()

    async def test_client_timeout_returns_timed_out(self, sandbox: K8sSandbox):
        async def handler(request):
            await asyncio.sleep(30)
            return web.Response(text="{}")

        runner, port = await _serve(handler)
        try:
            entry = _entry_for(sandbox, port=port, timeout=1)
            # Client budget = spec.timeout + 5 = 6s; patch to keep the test fast.
            entry.spec.timeout = -4  # total budget = 1s
            result = json.loads(await sandbox.execute("sb-test", "list_files", "{}"))
            assert result["timed_out"] is True
            assert entry.status == SandboxStatus.RUNNING  # tool-level, not infra
        finally:
            await runner.cleanup()
            await sandbox.aclose()

    async def test_500_returns_error_result(self, sandbox: K8sSandbox):
        async def handler(request):
            return web.Response(status=500, text="kaboom")

        runner, port = await _serve(handler)
        try:
            entry = _entry_for(sandbox, port=port)
            result = json.loads(await sandbox.execute("sb-test", "list_files", "{}"))
            assert result["exit_code"] == -1
            assert "500" in result["stderr"]
            assert entry.status == SandboxStatus.RUNNING
        finally:
            await runner.cleanup()
            await sandbox.aclose()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_k8s_sandbox.py::TestExecuteHttp -v`
Expected: FAIL — current `execute()` goes through `_exec_in_pod` (K8s exec),
and `aclose()` doesn't exist.

- [x] **Step 3: Rewrite execute() and delete the exec machinery**

In `surogates/sandbox/kubernetes.py`:

(a) Replace the entire `execute` method (lines 176-224) with:

```python
    async def execute(self, sandbox_id: str, name: str, input: str) -> str:
        """Execute a tool in the sandbox pod via the executor daemon.

        POSTs to the daemon on the pod IP.  Handler errors come back as
        200 + result JSON (the daemon catches them); HTTP/transport
        failures here mean the daemon itself is unreachable or broken.
        """
        entry = self._get_entry(sandbox_id)
        url = f"http://{entry.pod_ip}:{self._executor_port}/execute"
        try:
            args = json.loads(input) if input else {}
        except json.JSONDecodeError:
            args = {}

        session = await self._get_http()
        try:
            async with session.post(
                url,
                json={"name": name, "args": args, "timeout": entry.spec.timeout},
                headers={"Authorization": f"Bearer {entry.token}"},
                # ``connect=10`` makes a blackholed pod IP (node gone)
                # fail fast as a connection error instead of burning the
                # whole tool budget before failing.
                timeout=aiohttp.ClientTimeout(
                    total=entry.spec.timeout + 5, connect=10,
                ),
            ) as resp:
                body = await resp.text()
                if resp.status == 401:
                    # Token mismatch: the pod predates this worker's entry
                    # (or vice versa).  Unusable — reprovision.
                    entry.status = SandboxStatus.FAILED
                    raise SandboxUnavailableError(
                        f"Executor daemon in pod {entry.pod_name} rejected "
                        f"the sandbox token",
                    )
                if resp.status != 200:
                    logger.error(
                        "Executor daemon in pod %s returned HTTP %s: %s",
                        entry.pod_name, resp.status, body[:200],
                    )
                    return self._result_json(
                        exit_code=-1,
                        stdout="",
                        stderr=f"Executor daemon error (HTTP {resp.status})",
                        truncated=False,
                        timed_out=False,
                    )
                return body
        except aiohttp.ClientConnectionError as exc:
            # Daemon unreachable — pod gone, daemon dead, or an old
            # (pre-daemon) pod from before a deploy.  Every subsequent
            # sandbox tool would fail identically; mark FAILED so the
            # next ensure() reprovisions.
            #
            # ORDER MATTERS: this clause must come before TimeoutError.
            # aiohttp's connect-phase timeouts (ConnectionTimeoutError /
            # ServerTimeoutError) inherit BOTH ClientConnectionError and
            # TimeoutError — they mean "daemon unreachable" and must land
            # here, not in the tool-timeout branch below (which would
            # leave a dead sandbox marked healthy forever).
            logger.error(
                "Sandbox daemon unreachable in pod %s: %s", entry.pod_name, exc,
            )
            entry.status = SandboxStatus.FAILED
            raise SandboxUnavailableError(
                f"Sandbox daemon unreachable in pod {entry.pod_name} "
                f"(pod terminated, daemon dead, or pre-daemon image): {exc}",
            ) from exc
        except asyncio.TimeoutError:
            # Plain total-budget expiry while reading the response (the
            # connection succeeded, the tool is just slow).  The daemon
            # kills timed-out children itself; reaching the client-side
            # budget (+5s buffer) means it is unresponsive to the kill.
            logger.warning("Sandbox exec timed out in pod %s", entry.pod_name)
            return self._result_json(
                exit_code=-1,
                stdout="",
                stderr="Execution timed out",
                truncated=False,
                timed_out=True,
            )
```

(b) Add the HTTP session helpers right after `execute`:

```python
    async def _get_http(self) -> aiohttp.ClientSession:
        """Shared client session — connection pooling across tool calls."""
        if self._http is None:
            self._http = aiohttp.ClientSession()
        return self._http

    async def aclose(self) -> None:
        """Release the HTTP client session (worker shutdown)."""
        if self._http is not None:
            await self._http.close()
            self._http = None
```

(c) Delete the `_exec_in_pod` method entirely (lines ~441-510, including
its `WsApiClient` import and the result-parsing code below it that only it
used). Also delete `_classify_exec_failure` (line ~701) — every branch in
it is exec-API-specific (RBAC 401/403 on `pods/exec`, `ApiException`
mapping) and the new `execute()` carries its own messages. The
function-local `import aiohttp` inside the old `execute` disappears with
the rewrite (the module-top import from Task 7 serves the new code).

(d) In `surogates/orchestrator/worker.py`, in the shutdown `finally` block
(line ~1297), after `await sandbox_pool.destroy_all()` add:

```python
            backend_close = getattr(sandbox_backend, "aclose", None)
            if backend_close is not None:
                await backend_close()
```

(`sandbox_backend` is in scope — it's constructed in the same function at
line ~586. `ProcessSandbox` has no `aclose`, hence the getattr guard.)

- [x] **Step 4: Run the full sandbox test files**

Run: `uv run pytest tests/test_k8s_sandbox.py tests/test_sandbox.py tests/test_executor_server.py -v`
Expected: all PASS. Old exec-path tests in `test_k8s_sandbox.py` that mocked
`_exec_in_pod` will now fail to patch a missing attribute — delete those
tests (they tested deleted code; `TestExecuteHttp` is their replacement).

- [x] **Step 5: Run the wider suite for collateral damage**

Run: `uv run pytest tests/ -x -q 2>&1 | tail -20`
Expected: PASS (browser_e2e/live markers are excluded by default). Fix any
import fallout (e.g. a test importing `executor_path`).

- [x] **Step 6: Commit**

```bash
git add surogates/sandbox/kubernetes.py surogates/orchestrator/worker.py tests/test_k8s_sandbox.py
git commit -m "Dispatch sandbox tools over pod-IP HTTP and delete the K8s exec path"
```

---

### Task 9: NetworkPolicy manifest

`surogates-sandboxes` has no NetworkPolicy today, so the daemon port would be
reachable from any pod in the cluster. Token auth is the primary control;
this policy is hardening. Label facts from PROD: sandbox pods carry
`app=surogates-sandbox`; runtime-worker pods carry
`app.kubernetes.io/name=surogates-runtime` + `app.kubernetes.io/component=worker`;
the `surogates` namespace carries `kubernetes.io/metadata.name=surogates`.

**Files:**
- Create: `scripts/k8s/sandbox-executor-networkpolicy.yaml`

- [x] **Step 1: Write the manifest**

Create `scripts/k8s/sandbox-executor-networkpolicy.yaml`:

```yaml
# Restricts ingress to sandbox pods: only runtime-worker pods (in the
# `surogates` namespace) may reach the tool-executor daemon port.
# Token auth on the daemon is the primary control; this is hardening.
#
# Apply (PROD):
#   kubectl apply -f scripts/k8s/sandbox-executor-networkpolicy.yaml
#
# IMPORTANT: after applying, verify kubelet readiness probes still reach
# the daemon port under the cluster CNI — most CNIs exempt host/kubelet
# traffic from NetworkPolicy, but confirm it, don't assume it:
#   kubectl get pods -n surogates-sandboxes \
#     -o custom-columns=NAME:.metadata.name,READY:.status.conditions[?(@.type==\"Ready\")].status
# A freshly provisioned sandbox must reach Ready=True. If probes are
# blocked, the CNI needs a host-traffic exemption before this policy
# can stay.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: sandbox-executor-ingress
  namespace: surogates-sandboxes
spec:
  podSelector:
    matchLabels:
      app: surogates-sandbox
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: surogates
          podSelector:
            matchLabels:
              app.kubernetes.io/name: surogates-runtime
              app.kubernetes.io/component: worker
      ports:
        - protocol: TCP
          port: 8071
```

- [x] **Step 2: Validate the manifest parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('scripts/k8s/sandbox-executor-networkpolicy.yaml')); print('ok')"`
Expected: `ok`

- [x] **Step 3: Commit**

```bash
git add scripts/k8s/sandbox-executor-networkpolicy.yaml
git commit -m "Add NetworkPolicy restricting sandbox executor ingress to runtime workers"
```

---

### Task 10: Docs, integration test, full verification

**Files:**
- Modify: `CLAUDE.md:90`
- Modify: `docs/architecture/index.md:87`
- Create: `tests/test_executor_integration.py`

- [ ] **Step 1: Update the architecture docs**

In `CLAUDE.md` line ~90, replace:

```
(sandbox is `sleep infinity` + k8s-exec'd `tool-executor`; s3fs runs **geesefs** despite the name; browser is a forked `onkernel/chromium-headful`)
```

with:

```
(sandbox runs the persistent `tool-executor` daemon — `surogates.sandbox.executor_server`, serving tool calls over pod-IP HTTP with per-sandbox token auth; s3fs runs **geesefs** despite the name; browser is a forked `onkernel/chromium-headful`)
```

In `docs/architecture/index.md` line ~87, replace:

```
The sandbox runs the full `surogates` Python package. A `tool-executor` script accepts tool calls, dispatches them to real Python handlers via `ToolRegistry`, and returns JSON results over stdout.
```

with:

```
The sandbox runs the full `surogates` Python package. A persistent `tool-executor` daemon (`surogates.sandbox.executor_server`) loads `ToolRegistry` once at pod startup, then serves tool calls over HTTP on the pod IP; each call forks a child process that runs the real Python handler and returns the JSON result. The worker authenticates with a per-sandbox bearer token, and a mount-gated readiness probe ensures pod-Ready means "registry warm + workspace mounted".
```

Also sweep for stale references:

Run: `grep -rn "sleep infinity\|exec'd\|k8s-exec" CLAUDE.md docs/architecture/ --include="*.md" | grep -iv "browser"`
Update any remaining hit that describes the sandbox dispatch path
(specs/plans under `docs/superpowers/` are historical records — leave them).

- [ ] **Step 2: Write the opt-in live integration test**

Create `tests/test_executor_integration.py`:

```python
"""Live K8s integration for the persistent executor daemon (opt-in).

Provisions a real sandbox pod, asserts warm-tool latency and batch
overlap, then verifies unreachable-daemon classification.

Requires a cluster reachable via kubeconfig plus S3 settings for a
scratch workspace prefix:

    SUROGATES_K8S_INTEGRATION=1 \
    SANDBOX_NAMESPACE=surogates-sandboxes \
    SANDBOX_SERVICE_ACCOUNT=surogates-sandbox \
    SANDBOX_IMAGE=ghcr.io/invergent-ai/surogates-agent-sandbox:dev \
    S3FS_IMAGE=ghcr.io/invergent-ai/surogates-s3fs:latest \
    S3_ENDPOINT=http://... S3_ACCESS_KEY=... S3_SECRET_KEY=... \
    S3_WORKSPACE_REF=s3://bucket/executor-integration-test/ \
    uv run pytest tests/test_executor_integration.py -m live -v
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SUROGATES_K8S_INTEGRATION") != "1",
        reason="set SUROGATES_K8S_INTEGRATION=1 to run against a live cluster",
    ),
]


@pytest.fixture()
def live_sandbox():
    from surogates.sandbox.kubernetes import K8sSandbox

    return K8sSandbox(
        namespace=os.environ.get("SANDBOX_NAMESPACE", "surogates-sandboxes"),
        service_account=os.environ.get("SANDBOX_SERVICE_ACCOUNT", "surogates-sandbox"),
        pod_ready_timeout=120,
        executor_port=8071,
        storage_settings=SimpleNamespace(
            endpoint=os.environ["S3_ENDPOINT"],
            access_key=os.environ["S3_ACCESS_KEY"],
            secret_key=os.environ["S3_SECRET_KEY"],
            region=os.environ.get("S3_REGION", ""),
        ),
        s3fs_image=os.environ["S3FS_IMAGE"],
        s3_endpoint=os.environ["S3_ENDPOINT"],
    )


async def test_latency_overlap_and_failure_classification(live_sandbox):
    from surogates.sandbox.base import (
        Resource,
        SandboxSpec,
        SandboxUnavailableError,
    )

    spec = SandboxSpec(
        image=os.environ["SANDBOX_IMAGE"],
        resources=[Resource(
            source_ref=os.environ["S3_WORKSPACE_REF"],
            mount_path="/workspace",
        )],
    )
    sandbox_id = await live_sandbox.provision(spec)
    try:
        list_args = json.dumps({"pattern": "*"})

        # Warm single-call latency: must be sub-second-ish (allow 2s
        # headroom for cold geesefs metadata on the very first listing).
        start = time.monotonic()
        result = await live_sandbox.execute(sandbox_id, "list_files", list_args)
        first = time.monotonic() - start
        assert "error" not in json.loads(result) or json.loads(result).get("matches") is not None
        assert first < 5, f"first warm call too slow: {first:.2f}s"

        start = time.monotonic()
        await live_sandbox.execute(sandbox_id, "list_files", list_args)
        single = time.monotonic() - start
        assert single < 1.0, f"warm list_files too slow: {single:.2f}s"

        # Batch of 4 must overlap: wall ~= max, not sum.
        start = time.monotonic()
        await asyncio.gather(*[
            live_sandbox.execute(sandbox_id, "list_files", list_args)
            for _ in range(4)
        ])
        batch = time.monotonic() - start
        assert batch < single * 2.5 + 0.5, (
            f"batch serialized: 4 calls took {batch:.2f}s vs single {single:.2f}s"
        )

        # Kill the pod out-of-band -> next call must classify as
        # SandboxUnavailableError (connect failure), not hang.
        api = await live_sandbox._get_api()
        entry = live_sandbox._pods[sandbox_id]
        await api.delete_namespaced_pod(
            entry.pod_name, entry.namespace, grace_period_seconds=0,
        )
        await asyncio.sleep(5)
        with pytest.raises(SandboxUnavailableError):
            await live_sandbox.execute(sandbox_id, "list_files", list_args)
    finally:
        try:
            await live_sandbox.destroy(sandbox_id)
        except Exception:
            pass
        await live_sandbox.aclose()
```

- [ ] **Step 3: Verify the integration test is collected but skipped by default**

Run: `uv run pytest tests/test_executor_integration.py -v`
Expected: `no tests ran` (deselected — the `live` marker is excluded by the
default `addopts` in pyproject.toml)

Run: `SUROGATES_K8S_INTEGRATION= uv run pytest tests/test_executor_integration.py -m live -v`
Expected: 1 skipped (env gate)

- [ ] **Step 4: Build the sandbox image and run the live test (local cluster)**

```bash
docker build -t ghcr.io/invergent-ai/surogates-agent-sandbox:dev -f images/sandbox/Dockerfile .
k3d image import ghcr.io/invergent-ai/surogates-agent-sandbox:dev -c surogates
```

Then run the live test with the env block from the test docstring, filling
S3 values from `config.dev.yaml` (local Garage endpoint + credentials).
Expected: PASS — warm `list_files` < 1 s, batch of 4 ≈ max not sum, pod-kill
classified as `SandboxUnavailableError`.

- [ ] **Step 5: Full suite + lint**

```bash
uv run pytest tests/ -q 2>&1 | tail -5
uv run ruff check surogates/sandbox/ tests/test_executor_server.py
```

Expected: tests PASS; ruff clean (fix anything it flags).

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md docs/architecture/index.md tests/test_executor_integration.py
git commit -m "Document the persistent executor and add a live latency integration test"
```

---

## Deploy notes (PROD, after merge)

1. Release builds the worker + sandbox images together (same repo, same tag).
2. Apply `scripts/k8s/sandbox-executor-networkpolicy.yaml` and verify a fresh
   sandbox reaches `Ready=True` (kubelet probe reachability under the CNI).
3. During the deploy window, in-flight sessions on old (pre-daemon) pods get
   one `SandboxUnavailableError` tool result, then self-heal via reprovision.
4. Success check (compare against the baseline in the spec): warm
   `list_files`/`read_file`/`search_files` p50 < 1 s in the `events` table
   (`tool.result` → `elapsed_ms`), batches no longer cumulative.
