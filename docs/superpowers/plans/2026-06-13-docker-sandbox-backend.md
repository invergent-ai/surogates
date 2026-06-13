# Docker Sandbox Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third sandbox backend, `DockerSandbox`, that runs the agent-sandbox image as one Docker container per root session for local development, talking to the in-container tool-executor daemon over the same HTTP contract `K8sSandbox` uses.

**Architecture:** Extract the worker→daemon HTTP transport from `K8sSandbox` into a shared `ExecutorHTTPClient`; build `DockerSandbox` on the browser backend's `docker run` lifecycle pattern; wire it through config, the spec builder, the pool, and the worker. `K8sSandbox` is refactored only to delegate to the shared client (behavior-preserving).

**Tech Stack:** Python 3.12, `asyncio`, `aiohttp` (daemon HTTP), `httpx` (readiness probe), `pytest` with `aiohttp.web` test servers and an injected Docker driver. Spec: `docs/superpowers/specs/2026-06-13-docker-sandbox-backend-design.md`.

---

## Progress

Status is updated before each commit. Legend: `[ ]` pending · `[~]` in progress · `[x]` complete.

- [x] Task 1: `ExecutorHTTPClient` — shared daemon HTTP transport
- [x] Task 2: Refactor `K8sSandbox` to delegate to `ExecutorHTTPClient`
- [x] Task 3: Add `session_id` and `workspace_path` to `SandboxSpec`
- [x] Task 4: `executor_server` — `TOOL_EXECUTOR_REQUIRE_FUSE`
- [~] Task 5: `SandboxSettings` config — docker backend + fields
- [ ] Task 6: `DockerSandbox` core — lifecycle + execute
- [ ] Task 7: `DockerSandbox` — MCP proxy + KB env wiring
- [ ] Task 8: `SandboxPool.destroy_for_session` optional backend hook
- [ ] Task 9: Spec builder sets `session_id` and `workspace_path`
- [ ] Task 10: Worker wires the `docker` backend branch
- [ ] Task 11: Opt-in integration smoke test (real Docker)

---

## File Structure

**New files**
- `surogates/sandbox/_executor_client.py` — `ExecutorHTTPClient`: the shared, backend-agnostic worker→daemon HTTP transport (POST `/execute`, error taxonomy, result JSON, pooled `aiohttp` session).
- `surogates/sandbox/docker.py` — `DockerSandbox`: container lifecycle (provision/execute/status/destroy/destroy_for_session/aclose), the injectable `_DockerDriver`, workspace bind-mount, and MCP/KB env wiring.
- `tests/test_executor_http_client.py` — unit tests for `ExecutorHTTPClient` against a real local `aiohttp` server.
- `tests/test_docker_sandbox.py` — unit tests for `DockerSandbox` with a fake Docker driver + local `aiohttp` server.
- `tests/integration/test_docker_sandbox_e2e.py` — opt-in real-Docker smoke test (skipped without a daemon).

**Modified files**
- `surogates/sandbox/kubernetes.py` — delegate `execute`/session lifecycle to `ExecutorHTTPClient`.
- `surogates/sandbox/base.py` — add `SandboxSpec.session_id` and `SandboxSpec.workspace_path`.
- `surogates/sandbox/executor_server.py` — add `TOOL_EXECUTOR_REQUIRE_FUSE` (default on) so `/healthz` works without a FUSE mount.
- `surogates/sandbox/__init__.py` — export `DockerSandbox`.
- `surogates/sandbox/pool.py` — `destroy_for_session` also calls an optional backend `destroy_for_session`.
- `surogates/config.py` — `SandboxSettings`: `backend` literal + docker fields.
- `surogates/orchestrator/worker.py` — `docker` backend branch.
- `surogates/harness/tool_exec.py` — populate `spec.session_id` and `spec.workspace_path`.
- `tests/test_tool_exec_sandbox_spec.py` — extend with session_id/workspace_path assertions.
- `tests/test_executor_server.py` — extend healthz tests for `require_fuse`.

> All commands run from the repo root `/work/surogates`. Do **not** use `uv run` here; use `pytest` directly (the surogates wheel is installed in editable/dev mode and `uv run` would reinstall the pinned wheel). Commit messages use conventional-commit prefixes and **no** `Co-Authored-By` trailer.

---

### Task 1: `ExecutorHTTPClient` — shared daemon HTTP transport

**Files:**
- Create: `surogates/sandbox/_executor_client.py`
- Test: `tests/test_executor_http_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_executor_http_client.py`:

```python
"""Tests for surogates.sandbox._executor_client.ExecutorHTTPClient."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from surogates.sandbox._executor_client import ExecutorHTTPClient
from surogates.sandbox.base import SandboxUnavailableError


async def _serve(handler) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_post("/execute", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, shutdown_timeout=0.5)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


class TestExecute:
    async def test_passthrough_and_auth_header(self):
        seen = {}

        async def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = await request.json()
            return web.Response(text='{"ok": true}', content_type="application/json")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            result = await client.execute(
                host="127.0.0.1", port=port, token="tok-abc",
                name="list_files", args_str='{"pattern": "*"}', timeout=5,
            )
            assert json.loads(result) == {"ok": True}
            assert seen["auth"] == "Bearer tok-abc"
            assert seen["body"] == {
                "name": "list_files", "args": {"pattern": "*"}, "timeout": 5,
            }
        finally:
            await runner.cleanup()
            await client.aclose()

    async def test_bad_json_args_become_empty_dict(self):
        seen = {}

        async def handler(request):
            seen["body"] = await request.json()
            return web.Response(text="{}", content_type="application/json")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            await client.execute(
                host="127.0.0.1", port=port, token="t",
                name="x", args_str="not-json", timeout=5,
            )
            assert seen["body"]["args"] == {}
        finally:
            await runner.cleanup()
            await client.aclose()

    async def test_401_raises_unavailable(self):
        async def handler(request):
            return web.Response(status=401, text="unauthorized")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            with pytest.raises(SandboxUnavailableError):
                await client.execute(
                    host="127.0.0.1", port=port, token="t",
                    name="x", args_str="{}", timeout=5,
                )
        finally:
            await runner.cleanup()
            await client.aclose()

    async def test_500_returns_error_result(self):
        async def handler(request):
            return web.Response(status=500, text="kaboom")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            result = json.loads(await client.execute(
                host="127.0.0.1", port=port, token="t",
                name="x", args_str="{}", timeout=5,
            ))
            assert result["exit_code"] == -1
            assert "500" in result["stderr"]
            assert result["timed_out"] is False
        finally:
            await runner.cleanup()
            await client.aclose()

    async def test_connection_refused_raises_unavailable(self):
        client = ExecutorHTTPClient()
        try:
            with pytest.raises(SandboxUnavailableError):
                # Nothing listens on port 1.
                await client.execute(
                    host="127.0.0.1", port=1, token="t",
                    name="x", args_str="{}", timeout=5,
                )
        finally:
            await client.aclose()

    async def test_timeout_returns_timed_out(self):
        async def handler(request):
            await asyncio.sleep(30)
            return web.Response(text="{}")

        runner, port = await _serve(handler)
        client = ExecutorHTTPClient()
        try:
            # total budget = timeout + 5; -4 → 1s so the test is fast.
            result = json.loads(await client.execute(
                host="127.0.0.1", port=port, token="t",
                name="x", args_str="{}", timeout=-4,
            ))
            assert result["timed_out"] is True
        finally:
            await runner.cleanup()
            await client.aclose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_executor_http_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.sandbox._executor_client'`.

- [ ] **Step 3: Write the implementation**

Create `surogates/sandbox/_executor_client.py`:

```python
"""Shared worker→executor-daemon HTTP transport.

Both the Kubernetes and Docker sandbox backends run the same in-sandbox
tool-executor daemon (``surogates.sandbox.executor_server``) and reach it
over HTTP with a per-sandbox bearer token.  This client owns that
transport: the pooled ``aiohttp`` session, the ``POST /execute`` call, the
standard result JSON, and the failure taxonomy.

It is deliberately stateless about *which* sandbox it is talking to —
callers pass ``host``/``port``/``token`` per call — and it never touches
backend bookkeeping.  Fatal conditions (token rejected, daemon
unreachable) raise :class:`SandboxUnavailableError`; the calling backend
catches that, marks its own entry failed, and re-raises.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from surogates.sandbox.base import SandboxStatus, SandboxUnavailableError

logger = logging.getLogger(__name__)

# Extra seconds added to the per-tool timeout for the client-side budget,
# and the connect-phase timeout that makes a blackholed host fail fast.
_BUDGET_SLACK = 5
_CONNECT_TIMEOUT = 10


class ExecutorHTTPClient:
    """Pooled HTTP client for the in-sandbox tool-executor daemon."""

    def __init__(self) -> None:
        self._http: aiohttp.ClientSession | None = None

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

    async def execute(
        self,
        *,
        host: str,
        port: int,
        token: str,
        name: str,
        args_str: str,
        timeout: int,
    ) -> str:
        """POST one tool call to the daemon and return its JSON result.

        Raises :class:`SandboxUnavailableError` when the daemon rejects the
        token (401) or is unreachable (connection error).  HTTP errors and
        tool-level timeouts come back as a result-JSON string (no raise).
        """
        url = f"http://{host}:{port}/execute"
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}

        session = await self._get_http()
        try:
            async with session.post(
                url,
                json={"name": name, "args": args, "timeout": timeout},
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(
                    total=timeout + _BUDGET_SLACK, connect=_CONNECT_TIMEOUT,
                ),
            ) as resp:
                body = await resp.text()
                if resp.status == 401:
                    raise SandboxUnavailableError(
                        f"Executor daemon at {host}:{port} rejected the "
                        f"sandbox token",
                    )
                if resp.status != 200:
                    logger.error(
                        "Executor daemon at %s:%s returned HTTP %s: %s",
                        host, port, resp.status, body[:200],
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
            # ORDER MATTERS: aiohttp connect-phase timeouts inherit from both
            # ClientConnectionError and TimeoutError; they mean "unreachable"
            # and must land here, not in the tool-timeout branch below.
            logger.error("Sandbox daemon unreachable at %s:%s: %s", host, port, exc)
            raise SandboxUnavailableError(
                f"Sandbox daemon unreachable at {host}:{port}: {exc}",
            ) from exc
        except asyncio.TimeoutError:
            logger.warning("Sandbox exec timed out at %s:%s", host, port)
            return self._result_json(
                exit_code=-1,
                stdout="",
                stderr="Execution timed out",
                truncated=False,
                timed_out=True,
            )

    @staticmethod
    def _result_json(
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        truncated: bool,
        timed_out: bool,
    ) -> str:
        """Build the standard sandbox result JSON (shared by all backends)."""
        return json.dumps({
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
            "timed_out": timed_out,
        })


# ``SandboxStatus`` is imported above so backends can ``from
# surogates.sandbox._executor_client import ExecutorHTTPClient`` and keep the
# status enum reference local; re-export keeps the import surface small.
__all__ = ["ExecutorHTTPClient", "SandboxStatus"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_executor_http_client.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add surogates/sandbox/_executor_client.py tests/test_executor_http_client.py
git commit -m "feat: add shared ExecutorHTTPClient for sandbox daemon transport"
```

---

### Task 2: Refactor `K8sSandbox` to delegate to `ExecutorHTTPClient`

**Files:**
- Modify: `surogates/sandbox/kubernetes.py`
- Test (regression): `tests/test_k8s_sandbox.py` (existing `TestExecuteHttp`,
  plus moving `TestResultJson` to the shared client)

- [ ] **Step 1: Run the existing K8s execute tests to confirm the baseline is green**

Run: `pytest tests/test_k8s_sandbox.py::TestExecuteHttp -v`
Expected: PASS (these are the regression suite; they must still pass after the refactor).

- [ ] **Step 2: Replace the HTTP session field with the shared client**

In `surogates/sandbox/kubernetes.py`, in `K8sSandbox.__init__`, replace:

```python
        self._api: client.CoreV1Api | None = None
        self._http: aiohttp.ClientSession | None = None
```

with:

```python
        self._api: client.CoreV1Api | None = None
        self._client = ExecutorHTTPClient()
```

Add the import near the other sandbox imports at the top of the file:

```python
from surogates.sandbox._executor_client import ExecutorHTTPClient
```

- [ ] **Step 3: Move the result-shape tests to `ExecutorHTTPClient`**

In `tests/test_k8s_sandbox.py`, add this import near the existing sandbox imports:

```python
from surogates.sandbox._executor_client import ExecutorHTTPClient
```

Then replace the existing `TestResultJson` class:

```python
class TestResultJson:
    """Standard result JSON builder."""

    def test_success(self):
        result = json.loads(K8sSandbox._result_json(
            exit_code=0, stdout="hello", stderr="", truncated=False, timed_out=False,
        ))
        assert result["exit_code"] == 0
        assert result["stdout"] == "hello"
        assert result["timed_out"] is False

    def test_timeout(self):
        result = json.loads(K8sSandbox._result_json(
            exit_code=-1, stdout="", stderr="timed out", truncated=False, timed_out=True,
        ))
        assert result["timed_out"] is True
        assert result["exit_code"] == -1
```

with:

```python
class TestResultJson:
    """Standard result JSON builder."""

    def test_success(self):
        result = json.loads(ExecutorHTTPClient._result_json(
            exit_code=0, stdout="hello", stderr="", truncated=False, timed_out=False,
        ))
        assert result["exit_code"] == 0
        assert result["stdout"] == "hello"
        assert result["timed_out"] is False

    def test_timeout(self):
        result = json.loads(ExecutorHTTPClient._result_json(
            exit_code=-1, stdout="", stderr="timed out", truncated=False, timed_out=True,
        ))
        assert result["timed_out"] is True
        assert result["exit_code"] == -1
```

- [ ] **Step 4: Replace `execute`, `_get_http`, `aclose`, and `_result_json`**

Replace the entire `execute` method (currently lines ~190–271) **and** the `_get_http` / `aclose` methods (currently lines ~273–283) with:

```python
    async def execute(self, sandbox_id: str, name: str, input: str) -> str:
        """Execute a tool in the sandbox pod via the executor daemon.

        Delegates the HTTP transport to the shared client.  On a fatal
        condition (token rejected, daemon unreachable) the client raises
        ``SandboxUnavailableError``; we mark the entry FAILED so the next
        ``SandboxPool.ensure`` reprovisions, then re-raise.
        """
        entry = self._get_entry(sandbox_id)
        try:
            return await self._client.execute(
                host=entry.pod_ip,
                port=self._executor_port,
                token=entry.token,
                name=name,
                args_str=input,
                timeout=entry.spec.timeout,
            )
        except SandboxUnavailableError:
            entry.status = SandboxStatus.FAILED
            raise

    async def aclose(self) -> None:
        """Release the shared HTTP client session (worker shutdown)."""
        await self._client.aclose()
```

Then delete the now-unused `_result_json` static method (currently ~lines 706–722). Confirm it has no other callers first:

Run: `grep -n "_result_json" surogates/sandbox/kubernetes.py`
Expected after deletion: no matches.

- [ ] **Step 5: Remove the now-unused `aiohttp` import if nothing references it**

Run: `grep -n "aiohttp" surogates/sandbox/kubernetes.py`
If the only remaining reference was the deleted code, remove the `import aiohttp` line. If `grep` still shows other uses, leave the import.

- [ ] **Step 6: Run the regression tests**

Run: `pytest tests/test_k8s_sandbox.py -v`
Expected: PASS (all, including `TestExecuteHttp` — identical result shapes, 401/connection errors still mark the entry `FAILED`, timeout still returns `timed_out`).

- [ ] **Step 7: Commit**

```bash
git add surogates/sandbox/kubernetes.py tests/test_k8s_sandbox.py
git commit -m "refactor: K8sSandbox delegates daemon HTTP to ExecutorHTTPClient"
```

---

### Task 3: Add `session_id` and `workspace_path` to `SandboxSpec`

**Files:**
- Modify: `surogates/sandbox/base.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sandbox.py`:

```python
def test_sandbox_spec_has_session_and_workspace_fields():
    from surogates.sandbox.base import SandboxSpec

    # Defaults keep existing call sites working.
    spec = SandboxSpec()
    assert spec.session_id == ""
    assert spec.workspace_path is None

    spec2 = SandboxSpec(session_id="root-123", workspace_path="/tmp/ws")
    assert spec2.session_id == "root-123"
    assert spec2.workspace_path == "/tmp/ws"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_sandbox.py::test_sandbox_spec_has_session_and_workspace_fields -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'session_id'`.

- [ ] **Step 3: Add the fields**

In `surogates/sandbox/base.py`, in the `SandboxSpec` dataclass, add the two fields after `env`:

```python
    env: dict[str, str] = field(default_factory=dict)
    # Root sandbox session key (set by the spec builder). Docker uses it for
    # container labels and stale-container cleanup; K8sSandbox ignores it.
    session_id: str = ""
    # Host-bindable workspace path when one exists. Docker bind-mounts it;
    # K8sSandbox ignores it (its workspace is mounted by the s3fs sidecar).
    workspace_path: str | None = None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_sandbox.py::test_sandbox_spec_has_session_and_workspace_fields -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/sandbox/base.py tests/test_sandbox.py
git commit -m "feat: add session_id and workspace_path to SandboxSpec"
```

---

### Task 4: `executor_server` — `TOOL_EXECUTOR_REQUIRE_FUSE`

**Files:**
- Modify: `surogates/sandbox/executor_server.py`
- Test: `tests/test_executor_server.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_executor_server.py` (the `_make_client` helper, `fake_registry` fixture, and `import json`/`import httpx` already exist in this file):

```python
class TestHealthzRequireFuse:
    async def test_healthz_ok_without_fuse_when_not_required(
        self, fake_registry, tmp_path
    ):
        # A mounts file with no FUSE entry at /workspace.
        mounts = tmp_path / "mounts"
        mounts.write_text("overlay / overlay rw 0 0\n")
        app = executor_server.create_app(
            token="t",
            workspace="/workspace",
            mounts_path=str(mounts),
            require_fuse=False,
        )
        async with _make_client(app) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200

    async def test_healthz_503_without_fuse_when_required(
        self, fake_registry, tmp_path
    ):
        mounts = tmp_path / "mounts"
        mounts.write_text("overlay / overlay rw 0 0\n")
        app = executor_server.create_app(
            token="t",
            workspace="/workspace",
            mounts_path=str(mounts),
            require_fuse=True,
        )
        async with _make_client(app) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 503
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_executor_server.py::TestHealthzRequireFuse -v`
Expected: FAIL with `TypeError: create_app() got an unexpected keyword argument 'require_fuse'`.

- [ ] **Step 3: Add the `require_fuse` parameter and honor it in `/healthz`**

In `surogates/sandbox/executor_server.py`, change the `create_app` signature to add `require_fuse`:

```python
def create_app(
    *,
    token: str,
    workspace: str,
    mounts_path: str = "/proc/mounts",
    max_concurrency: int = MAX_CONCURRENCY,
    default_timeout: int = DEFAULT_TIMEOUT,
    require_fuse: bool = True,
) -> FastAPI:
```

Replace the `healthz` handler body:

```python
    @app.get("/healthz")
    async def healthz() -> Response:
        # When the workspace is not a FUSE mount (Docker bind-mount or
        # ephemeral), the FUSE check is the wrong readiness signal; the
        # backend disables it via require_fuse=False.
        if not require_fuse or workspace_mounted(workspace, mounts_path):
            return Response(content="ok", status_code=200)
        return Response(content="workspace not mounted", status_code=503)
```

In `main()`, read the env var and pass it through:

```python
    token = os.environ.get("TOOL_EXECUTOR_TOKEN", "")
    if not token:
        logger.error("TOOL_EXECUTOR_TOKEN is required")
        sys.exit(1)

    require_fuse = os.environ.get("TOOL_EXECUTOR_REQUIRE_FUSE", "1") != "0"

    logger.info("Loading tool registry...")
    init_registry()
    logger.info("Registry loaded; serving on 0.0.0.0:%d", port)

    import uvicorn

    uvicorn.run(
        create_app(token=token, workspace=workspace, require_fuse=require_fuse),
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_executor_server.py::TestHealthzRequireFuse -v`
Expected: PASS (2 tests). Also run the full file to confirm no regression: `pytest tests/test_executor_server.py -v`.

- [ ] **Step 5: Commit**

```bash
git add surogates/sandbox/executor_server.py tests/test_executor_server.py
git commit -m "feat: add TOOL_EXECUTOR_REQUIRE_FUSE to executor_server healthz"
```

---

### Task 5: `SandboxSettings` config — `docker` backend + fields

**Files:**
- Modify: `surogates/config.py:320-356` (`SandboxSettings`)
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sandbox.py`:

```python
def test_sandbox_settings_docker_defaults():
    from surogates.config import SandboxSettings

    s = SandboxSettings()
    # backend literal accepts "docker"
    s2 = SandboxSettings(backend="docker")
    assert s2.backend == "docker"
    assert s.docker_image == "ghcr.io/invergent-ai/surogates-agent-sandbox:latest"
    assert s.docker_executor_port_base == 33000
    assert s.docker_ready_timeout == 60
    assert s.docker_network == "bridge"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_sandbox.py::test_sandbox_settings_docker_defaults -v`
Expected: FAIL — `backend="docker"` rejected by the `Literal`, or `AttributeError` on `docker_image`.

- [ ] **Step 3: Update `SandboxSettings`**

In `surogates/config.py`, change the `backend` field and add docker fields. Replace:

```python
    backend: Literal["process", "kubernetes"] = "process"
```

with:

```python
    backend: Literal["process", "kubernetes", "docker"] = "process"
```

Then add, after the `srt_settings_dir` line (before the K8s settings block):

```python
    # Docker sandbox backend settings (only used when backend == "docker").
    # Local-development backend: one container per root session, talking to
    # the in-container executor daemon over a published port.
    docker_image: str = "ghcr.io/invergent-ai/surogates-agent-sandbox:latest"
    # Host port base for the published executor port (host_port = base + offset).
    # Chosen to avoid the browser backend's 30000/31000/32000 bases.
    docker_executor_port_base: int = 33000
    # Seconds to wait for /healthz after docker run (matches k8s_pod_ready_timeout).
    docker_ready_timeout: int = 60
    docker_network: str = "bridge"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_sandbox.py::test_sandbox_settings_docker_defaults -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/config.py tests/test_sandbox.py
git commit -m "feat: add docker backend option and settings to SandboxSettings"
```

---

### Task 6: `DockerSandbox` core — lifecycle + execute

**Files:**
- Create: `surogates/sandbox/docker.py`
- Modify: `surogates/sandbox/__init__.py`
- Test: `tests/test_docker_sandbox.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_docker_sandbox.py`:

```python
"""Tests for surogates.sandbox.docker.DockerSandbox."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from aiohttp import web

from surogates.sandbox.base import SandboxSpec, SandboxStatus, SandboxUnavailableError
from surogates.sandbox.docker import DockerSandbox, _Entry


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._containers: dict[str, dict[str, Any]] = {}
        self.fail_next_run_with_port_conflict = False

    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        self.calls.append(args)
        if args[:2] == ["run", "-d"]:
            if self.fail_next_run_with_port_conflict:
                self.fail_next_run_with_port_conflict = False
                return (125, b"", b"Bind for 0.0.0.0:33000 failed: port is already allocated")
            cid = f"cid-{len(self._containers) + 1}"
            labels = {}
            for idx, arg in enumerate(args):
                if arg == "--label" and idx + 1 < len(args):
                    key, _, value = args[idx + 1].partition("=")
                    labels[key] = value
            self._containers[cid] = {"running": True, "labels": labels}
            return 0, cid.encode() + b"\n", b""
        if args[:2] == ["ps", "-aq"]:
            label = ""
            for idx, arg in enumerate(args):
                if arg == "--filter" and idx + 1 < len(args):
                    label = args[idx + 1].removeprefix("label=")
            key, _, value = label.partition("=")
            matches = [
                cid for cid, st in self._containers.items()
                if st.get("labels", {}).get(key) == value
            ]
            return 0, ("\n".join(matches) + ("\n" if matches else "")).encode(), b""
        if args[0] == "inspect":
            cid = args[-1]
            running = self._containers.get(cid, {}).get("running", False)
            return 0, (b"running" if running else b"exited") + b"\n", b""
        if args[0] in {"stop", "rm"}:
            cid = args[-1]
            if cid in self._containers and args[0] == "rm":
                del self._containers[cid]
            return 0, b"", b""
        return 0, b"", b""


@pytest.fixture()
def healthz_transport():
    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/healthz":
                return httpx.Response(200, text="ok")
            return httpx.Response(404)
    return T()


def _backend(docker, healthz_transport, **kw):
    return DockerSandbox(
        image="sbx-test:1",
        executor_port_base=33000,
        ready_timeout=5,
        network="bridge",
        docker=docker,
        httpx_transport=healthz_transport,
        **kw,
    )


async def _serve(handler) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_post("/execute", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, shutdown_timeout=0.5)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


class TestProvision:
    async def test_runs_docker_and_records_entry(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        sid = await backend.provision(SandboxSpec(session_id="root-1"))
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        joined = " ".join(run_call)
        assert "33000:8071" in joined
        assert "--network bridge" in joined
        assert "host.docker.internal:host-gateway" in joined
        assert "app=surogates-sandbox" in joined
        assert "surogates.session_id=root-1" in joined
        assert "TOOL_EXECUTOR_REQUIRE_FUSE=0" in joined
        assert any(a.startswith("TOOL_EXECUTOR_TOKEN=") for a in run_call)
        assert run_call[-1] == "sbx-test:1"
        assert backend._entries[sid].host_port == 33000
        await backend.aclose()

    async def test_reaps_stale_session_containers_before_provision(self, healthz_transport):
        docker = FakeDocker()
        # Pre-seed a stale container labelled for the same session.
        docker._containers["stale"] = {
            "running": True, "labels": {"surogates.session_id": "root-1"},
        }
        backend = _backend(docker, healthz_transport)
        await backend.provision(SandboxSpec(session_id="root-1"))
        # The stale container was listed and removed before the new run.
        assert any(c[:2] == ["ps", "-aq"] for c in docker.calls)
        assert "stale" not in docker._containers
        await backend.aclose()

    async def test_retries_next_port_on_conflict(self, healthz_transport):
        docker = FakeDocker()
        docker.fail_next_run_with_port_conflict = True
        backend = _backend(docker, healthz_transport)
        sid = await backend.provision(SandboxSpec(session_id="root-1"))
        run_calls = [c for c in docker.calls if c[:2] == ["run", "-d"]]
        assert len(run_calls) == 2
        assert "33000:8071" in " ".join(run_calls[0])
        assert "33001:8071" in " ".join(run_calls[1])
        assert backend._entries[sid].host_port == 33001
        await backend.aclose()

    async def test_run_failure_raises_unavailable(self, healthz_transport):
        class BrokenDocker(FakeDocker):
            async def run(self, args):
                self.calls.append(args)
                if args[:2] == ["run", "-d"]:
                    return 1, b"", b"Cannot connect to the Docker daemon"
                return 0, b"", b""
        backend = _backend(BrokenDocker(), healthz_transport)
        with pytest.raises(SandboxUnavailableError):
            await backend.provision(SandboxSpec(session_id="root-1"))
        await backend.aclose()


class TestWorkspaceMount:
    async def test_binds_workspace_when_path_valid(self, healthz_transport, tmp_path):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        await backend.provision(
            SandboxSpec(session_id="root-1", workspace_path=str(tmp_path))
        )
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        assert f"{tmp_path}:/workspace" in " ".join(run_call)
        await backend.aclose()

    async def test_no_mount_for_sentinel_or_missing(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        await backend.provision(
            SandboxSpec(session_id="root-1", workspace_path="/workspace")
        )
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        assert ":/workspace" not in " ".join(
            a for a in run_call if a != "/workspace"
        )
        # Explicit: no -v flag emitted.
        assert "-v" not in run_call
        await backend.aclose()


class TestExecute:
    async def test_passthrough(self, healthz_transport):
        async def handler(request):
            return web.Response(text='{"ok": true}', content_type="application/json")
        runner, port = await _serve(handler)
        backend = _backend(FakeDocker(), healthz_transport)
        backend._entries["sb"] = _Entry(
            sandbox_id="sb", container_id="cid-1", host_port=port,
            token="t", spec=SandboxSpec(timeout=5),
        )
        try:
            result = await backend.execute("sb", "terminal", '{"command": "ls"}')
            assert json.loads(result) == {"ok": True}
        finally:
            await runner.cleanup()
            await backend.aclose()

    async def test_401_marks_failed_and_raises(self, healthz_transport):
        async def handler(request):
            return web.Response(status=401, text="no")
        runner, port = await _serve(handler)
        backend = _backend(FakeDocker(), healthz_transport)
        entry = _Entry(
            sandbox_id="sb", container_id="cid-1", host_port=port,
            token="t", spec=SandboxSpec(timeout=5),
        )
        backend._entries["sb"] = entry
        try:
            with pytest.raises(SandboxUnavailableError):
                await backend.execute("sb", "terminal", "{}")
            assert entry.status == SandboxStatus.FAILED
        finally:
            await runner.cleanup()
            await backend.aclose()

    async def test_unknown_sandbox_raises_value_error(self, healthz_transport):
        backend = _backend(FakeDocker(), healthz_transport)
        with pytest.raises(ValueError):
            await backend.execute("nope", "terminal", "{}")
        await backend.aclose()


class TestStatusAndDestroy:
    async def test_status_running(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        sid = await backend.provision(SandboxSpec(session_id="root-1"))
        assert await backend.status(sid) == SandboxStatus.RUNNING
        await backend.aclose()

    async def test_status_unknown_is_terminated(self, healthz_transport):
        backend = _backend(FakeDocker(), healthz_transport)
        assert await backend.status("nope") == SandboxStatus.TERMINATED
        await backend.aclose()

    async def test_destroy_stops_and_removes(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        sid = await backend.provision(SandboxSpec(session_id="root-1"))
        await backend.destroy(sid)
        assert sid not in backend._entries
        assert any(c[0] == "stop" for c in docker.calls)
        assert any(c[0] == "rm" for c in docker.calls)
        await backend.aclose()

    async def test_destroy_for_session_filters_by_label(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        await backend.provision(SandboxSpec(session_id="root-1"))
        docker.calls.clear()
        await backend.destroy_for_session("root-1")
        assert any(c[:2] == ["ps", "-aq"] for c in docker.calls)
        assert any(c[0] == "rm" for c in docker.calls)
        await backend.aclose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_docker_sandbox.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.sandbox.docker'`.

- [ ] **Step 3: Write the implementation**

Create `surogates/sandbox/docker.py`:

```python
"""Docker sandbox backend — one container per root session (local dev).

Runs the agent-sandbox image (whose main process is the tool-executor
daemon) as a Docker container and talks to it over HTTP via the shared
:class:`ExecutorHTTPClient`, mirroring the K8s backend's executor contract
through the browser backend's ``docker run`` lifecycle.  Intended for
local development on a trusted single-user host; production multi-tenant
isolation remains the Kubernetes backend's job.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from surogates.sandbox._executor_client import ExecutorHTTPClient
from surogates.sandbox.base import (
    SandboxSpec,
    SandboxStatus,
    SandboxUnavailableError,
)

logger = logging.getLogger(__name__)

# The image's TOOL_EXECUTOR_PORT default; the daemon always binds this
# fixed in-container port and only the published host port varies.
_IN_CONTAINER_PORT = 8071
_WORKSPACE_SENTINEL = "/workspace"
_PORT_CONFLICT_RE = re.compile(
    r"(port is already allocated|Bind for .* failed)", re.IGNORECASE,
)
_MAX_PORT_ATTEMPTS = 25


def _rewrite_host_for_container(url: str) -> str:
    """Rewrite host-local URLs so a bridged container can reach host services."""
    return url.replace("127.0.0.1", "host.docker.internal").replace(
        "localhost", "host.docker.internal",
    )


class _DockerDriver(Protocol):
    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]: ...


class _RealDocker:
    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode or 0, stdout, stderr


@dataclass(slots=True)
class _Entry:
    sandbox_id: str
    container_id: str
    host_port: int
    token: str
    spec: SandboxSpec
    status: SandboxStatus = SandboxStatus.RUNNING


class DockerSandbox:
    """Sandbox backend that runs one Docker container per root session."""

    def __init__(
        self,
        *,
        image: str = "ghcr.io/invergent-ai/surogates-agent-sandbox:latest",
        executor_port_base: int = 33000,
        ready_timeout: int = 60,
        network: str = "bridge",
        mcp_proxy_url: str = "",
        docker: _DockerDriver | None = None,
        httpx_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._image = image
        self._port_base = executor_port_base
        self._ready_timeout = ready_timeout
        self._network = network
        self._mcp_proxy_url = mcp_proxy_url
        self._docker = docker or _RealDocker()
        self._transport = httpx_transport
        self._client = ExecutorHTTPClient()
        self._entries: dict[str, _Entry] = {}
        self._next_offset = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Sandbox protocol
    # ------------------------------------------------------------------

    async def provision(self, spec: SandboxSpec) -> str:
        sandbox_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)

        # Reap stale containers left by a previous worker for this root
        # session before claiming a new one.
        if spec.session_id:
            await self.destroy_for_session(spec.session_id)

        workspace = self._mountable_workspace(spec.workspace_path)
        env = self._build_env(spec, sandbox_id, token)
        image = spec.image or self._image

        container_id = ""
        for _attempt in range(_MAX_PORT_ATTEMPTS):
            async with self._lock:
                offset = self._next_offset
                self._next_offset += 1
            host_port = self._port_base + offset

            args = ["run", "-d", "--rm", "-p", f"{host_port}:{_IN_CONTAINER_PORT}"]
            if self._network:
                args += ["--network", self._network]
            args += [
                "--add-host", "host.docker.internal:host-gateway",
                "--label", "app=surogates-sandbox",
            ]
            if spec.session_id:
                args += ["--label", f"surogates.session_id={spec.session_id}"]
            if workspace is not None:
                args += ["-v", f"{workspace}:/workspace"]
            for key, value in env.items():
                args += ["-e", f"{key}={value}"]
            args.append(image)

            code, stdout, stderr = await self._docker.run(args)
            if code != 0:
                stderr_text = stderr.decode(errors="replace")
                if _PORT_CONFLICT_RE.search(stderr_text):
                    logger.warning(
                        "Sandbox port %d unavailable; trying next offset", host_port,
                    )
                    continue
                raise SandboxUnavailableError(
                    f"docker run failed (exit {code}): {stderr_text}",
                    classification="docker",
                )
            container_id = stdout.decode().strip().splitlines()[0]
            break
        else:
            raise SandboxUnavailableError(
                "docker run failed: no free sandbox ports found",
                classification="docker",
            )

        try:
            await self._wait_ready(host_port)
        except Exception:
            await self._docker.run(["stop", container_id])
            await self._docker.run(["rm", container_id])
            raise

        self._entries[sandbox_id] = _Entry(
            sandbox_id=sandbox_id,
            container_id=container_id,
            host_port=host_port,
            token=token,
            spec=spec,
        )
        logger.info(
            "Provisioned docker sandbox %s (container %s, port %d)",
            sandbox_id, container_id, host_port,
        )
        return sandbox_id

    async def execute(self, sandbox_id: str, name: str, input: str) -> str:
        entry = self._entries.get(sandbox_id)
        if entry is None:
            raise ValueError(f"Unknown sandbox: {sandbox_id}")
        try:
            return await self._client.execute(
                host="127.0.0.1",
                port=entry.host_port,
                token=entry.token,
                name=name,
                args_str=input,
                timeout=entry.spec.timeout,
            )
        except SandboxUnavailableError:
            entry.status = SandboxStatus.FAILED
            raise

    async def status(self, sandbox_id: str) -> SandboxStatus:
        entry = self._entries.get(sandbox_id)
        if entry is None:
            return SandboxStatus.TERMINATED
        code, stdout, _stderr = await self._docker.run(
            ["inspect", "--format", "{{.State.Status}}", entry.container_id],
        )
        if code != 0:
            return SandboxStatus.FAILED
        status = stdout.decode().strip()
        if status == "running":
            return SandboxStatus.RUNNING
        if status in {"created", "restarting"}:
            return SandboxStatus.PENDING
        if status in {"exited", "dead", "removing"}:
            return SandboxStatus.TERMINATED
        return SandboxStatus.FAILED

    async def destroy(self, sandbox_id: str) -> None:
        entry = self._entries.pop(sandbox_id, None)
        if entry is None:
            return
        await self._docker.run(["stop", entry.container_id])
        await self._docker.run(["rm", entry.container_id])
        logger.info("Destroyed docker sandbox %s", sandbox_id)

    async def destroy_for_session(self, session_id: str) -> None:
        code, stdout, stderr = await self._docker.run(
            ["ps", "-aq", "--filter", f"label=surogates.session_id={session_id}"],
        )
        if code != 0:
            logger.warning(
                "Failed to list sandbox containers for session %s: %s",
                session_id, stderr.decode(errors="replace"),
            )
            return
        for container_id in stdout.decode().split():
            # Drop any in-memory entry pointing at this container.
            for sid, entry in list(self._entries.items()):
                if entry.container_id == container_id:
                    self._entries.pop(sid, None)
            await self._docker.run(["stop", container_id])
            await self._docker.run(["rm", container_id])
            logger.info(
                "Destroyed sandbox container %s for session %s",
                container_id, session_id,
            )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_env(
        self, spec: SandboxSpec, sandbox_id: str, token: str,
    ) -> dict[str, str]:
        """Base container env. MCP/KB host wiring is layered on in Task 7."""
        env = {
            "TOOL_EXECUTOR_TOKEN": token,
            "WORKSPACE_DIR": "/workspace",
            "TOOL_EXECUTOR_REQUIRE_FUSE": "0",
        }
        reserved = {
            "TOOL_EXECUTOR_TOKEN", "WORKSPACE_DIR",
            "TOOL_EXECUTOR_REQUIRE_FUSE", "TOOL_EXECUTOR_PORT",
        }
        for key, value in spec.env.items():
            if key not in reserved:
                env[key] = value
        return env

    def _mountable_workspace(self, workspace_path: str | None) -> Path | None:
        # "/workspace" is the in-pod FUSE sentinel returned by S3Backend; it is
        # not bindable from the host. Empty/None means no workspace path.
        if not workspace_path or workspace_path == _WORKSPACE_SENTINEL:
            return None
        workspace = Path(workspace_path).resolve()
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Skipping sandbox workspace bind mount for %s: %s", workspace, exc,
            )
            return None
        if not workspace.is_dir():
            return None
        return workspace

    async def _wait_ready(self, host_port: int) -> None:
        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        last_error: Exception | None = None
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{host_port}",
            transport=self._transport,
            timeout=2.0,
        ) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get("/healthz")
                    if response.status_code == 200:
                        return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                await asyncio.sleep(0.5)
        detail = type(last_error).__name__ if last_error is not None else "no_response"
        raise SandboxUnavailableError(
            f"Sandbox did not become ready within {self._ready_timeout}s ({detail})",
            classification="readiness",
        )
```

- [ ] **Step 4: Export `DockerSandbox`**

In `surogates/sandbox/__init__.py`, add the import and `__all__` entry:

```python
from surogates.sandbox.base import Resource, Sandbox, SandboxSpec, SandboxStatus
from surogates.sandbox.docker import DockerSandbox
from surogates.sandbox.kubernetes import K8sSandbox
from surogates.sandbox.pool import SandboxPool
from surogates.sandbox.process import ProcessSandbox

__all__ = [
    "DockerSandbox",
    "K8sSandbox",
    "ProcessSandbox",
    "Resource",
    "Sandbox",
    "SandboxPool",
    "SandboxSpec",
    "SandboxStatus",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_docker_sandbox.py -v`
Expected: PASS (all classes: `TestProvision`, `TestWorkspaceMount`, `TestExecute`, `TestStatusAndDestroy`).

- [ ] **Step 6: Commit**

```bash
git add surogates/sandbox/docker.py surogates/sandbox/__init__.py tests/test_docker_sandbox.py
git commit -m "feat: add DockerSandbox backend (lifecycle + execute)"
```

---

### Task 7: `DockerSandbox` — MCP proxy + KB env wiring

**Files:**
- Modify: `surogates/sandbox/docker.py` (`_build_env`, add `_mint_mcp_token`)
- Test: `tests/test_docker_sandbox.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_docker_sandbox.py`:

```python
class TestHostServiceEnv:
    async def test_mcp_url_rewritten_and_token_injected(
        self, healthz_transport, monkeypatch
    ):
        monkeypatch.setattr(
            "surogates.tenant.auth.jwt.create_sandbox_token",
            lambda **kw: "mcp-tok",
        )
        docker = FakeDocker()
        backend = _backend(
            docker, healthz_transport,
            mcp_proxy_url="http://localhost:8001",
        )
        spec = SandboxSpec(
            session_id="11111111-1111-1111-1111-111111111111",
            env={
                "ORG_ID": "22222222-2222-2222-2222-222222222222",
                "USER_ID": "33333333-3333-3333-3333-333333333333",
                "SUROGATES_AGENT_ID": "agent-9",
            },
        )
        await backend.provision(spec)
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        joined = " ".join(run_call)
        assert "MCP_PROXY_URL=http://host.docker.internal:8001" in joined
        assert "MCP_PROXY_TOKEN=mcp-tok" in joined
        await backend.aclose()

    async def test_kb_env_passed_with_url_rewrite(
        self, healthz_transport, monkeypatch
    ):
        monkeypatch.setenv("SUROGATES_OPS_DB_URL", "postgresql://localhost:5432/ops")
        monkeypatch.setenv("SUROGATES_KB_HUB_ACCESS_KEY_ID", "ak-1")
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        await backend.provision(SandboxSpec(session_id="root-1"))
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        joined = " ".join(run_call)
        assert "SUROGATES_OPS_DB_URL=postgresql://host.docker.internal:5432/ops" in joined
        assert "SUROGATES_KB_HUB_ACCESS_KEY_ID=ak-1" in joined
        await backend.aclose()

    async def test_no_mcp_env_when_proxy_url_unset(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)  # mcp_proxy_url defaults to ""
        await backend.provision(SandboxSpec(session_id="root-1"))
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        assert "MCP_PROXY_URL=" not in " ".join(run_call)
        await backend.aclose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_docker_sandbox.py::TestHostServiceEnv -v`
Expected: FAIL — `MCP_PROXY_URL` / KB env assertions fail because `_build_env` does not yet inject them.

- [ ] **Step 3: Extend `_build_env` and add `_mint_mcp_token`**

In `surogates/sandbox/docker.py`, replace the entire `_build_env` method with this version, and add `_mint_mcp_token` directly after it:

```python
    def _build_env(
        self, spec: SandboxSpec, sandbox_id: str, token: str,
    ) -> dict[str, str]:
        """Container env: base + spec passthrough + MCP/KB host wiring.

        Mirrors the K8s pod manifest's env block, with host-local URLs
        rewritten so a bridged container can reach host services.
        """
        import os

        env = {
            "TOOL_EXECUTOR_TOKEN": token,
            "WORKSPACE_DIR": "/workspace",
            "TOOL_EXECUTOR_REQUIRE_FUSE": "0",
        }
        reserved = {
            "TOOL_EXECUTOR_TOKEN", "WORKSPACE_DIR",
            "TOOL_EXECUTOR_REQUIRE_FUSE", "TOOL_EXECUTOR_PORT",
        }
        for key, value in spec.env.items():
            if key not in reserved:
                env[key] = value

        # MCP proxy — mirror the K8s pod manifest.
        if self._mcp_proxy_url:
            env["MCP_PROXY_URL"] = _rewrite_host_for_container(self._mcp_proxy_url)
            mcp_token = self._mint_mcp_token(spec, sandbox_id)
            if mcp_token:
                env["MCP_PROXY_TOKEN"] = mcp_token

        # KB env passthrough from the worker process, URLs rewritten for the
        # bridged container. Mirrors the K8s manifest's KB var loop.
        for kb_var in (
            "SUROGATES_AGENT_ID",
            "SUROGATES_OPS_DB_URL",
            "SUROGATES_KB_HUB_ENDPOINT_URL",
            "SUROGATES_KB_HUB_ACCESS_KEY_ID",
            "SUROGATES_KB_HUB_SECRET_ACCESS_KEY",
        ):
            val = os.environ.get(kb_var, "")
            if val:
                env[kb_var] = (
                    _rewrite_host_for_container(val)
                    if kb_var.endswith("_URL")
                    else val
                )
        return env

    def _mint_mcp_token(self, spec: SandboxSpec, sandbox_id: str) -> str:
        """Mint a sandbox→MCP-proxy token, mirroring the K8s manifest.

        Returns "" on any failure (e.g. non-UUID env in local dev) so a
        misconfigured MCP setup degrades to "MCP tools unavailable" rather
        than failing the whole provision.
        """
        from surogates.tenant.auth.jwt import create_sandbox_token

        zero = "00000000-0000-0000-0000-000000000000"
        try:
            session_uuid = (
                uuid.UUID(spec.session_id) if spec.session_id
                else uuid.UUID(sandbox_id)
            )
            return create_sandbox_token(
                org_id=uuid.UUID(spec.env.get("ORG_ID", zero)),
                user_id=uuid.UUID(spec.env.get("USER_ID", zero)),
                session_id=session_uuid,
                agent_id=spec.env.get("SUROGATES_AGENT_ID") or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not mint MCP proxy token for docker sandbox: %s", exc,
            )
            return ""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_docker_sandbox.py -v`
Expected: PASS (all, including `TestHostServiceEnv`).

> Note: `test_kb_env_passed_with_url_rewrite` and `test_no_mcp_env_when_proxy_url_unset` read `os.environ`. The `monkeypatch.setenv` in the KB test is scoped per-test, so the other tests (which do not set KB vars) see them unset — no cross-test leakage.

- [ ] **Step 5: Commit**

```bash
git add surogates/sandbox/docker.py tests/test_docker_sandbox.py
git commit -m "feat: wire MCP proxy and KB env into DockerSandbox"
```

---

### Task 8: `SandboxPool.destroy_for_session` calls the optional backend hook

**Files:**
- Modify: `surogates/sandbox/pool.py:130-146`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sandbox.py`:

```python
class _BackendWithReap:
    def __init__(self):
        self.destroyed_ids = []
        self.reaped_sessions = []

    async def provision(self, spec):
        return "sb-1"

    async def execute(self, sandbox_id, name, input):
        return "{}"

    async def status(self, sandbox_id):
        from surogates.sandbox.base import SandboxStatus
        return SandboxStatus.RUNNING

    async def destroy(self, sandbox_id):
        self.destroyed_ids.append(sandbox_id)

    async def destroy_for_session(self, session_id):
        self.reaped_sessions.append(session_id)


class _BackendNoReap:
    async def provision(self, spec):
        return "sb-1"

    async def execute(self, sandbox_id, name, input):
        return "{}"

    async def status(self, sandbox_id):
        from surogates.sandbox.base import SandboxStatus
        return SandboxStatus.RUNNING

    async def destroy(self, sandbox_id):
        pass


async def test_pool_destroy_for_session_calls_backend_reap():
    from surogates.sandbox.base import SandboxSpec
    from surogates.sandbox.pool import SandboxPool

    backend = _BackendWithReap()
    pool = SandboxPool(backend)
    await pool.ensure("root-1", SandboxSpec())
    await pool.destroy_for_session("root-1")
    assert backend.destroyed_ids == ["sb-1"]
    assert backend.reaped_sessions == ["root-1"]


async def test_pool_destroy_for_session_reaps_without_mapping():
    from surogates.sandbox.pool import SandboxPool

    backend = _BackendWithReap()
    pool = SandboxPool(backend)
    # No ensure() — pool has no mapping, but the backend should still reap.
    await pool.destroy_for_session("orphan-1")
    assert backend.destroyed_ids == []
    assert backend.reaped_sessions == ["orphan-1"]


async def test_pool_destroy_for_session_without_backend_reap_is_noop():
    from surogates.sandbox.pool import SandboxPool

    backend = _BackendNoReap()
    pool = SandboxPool(backend)
    # Backend has no destroy_for_session — must not raise.
    await pool.destroy_for_session("root-1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_sandbox.py -k destroy_for_session -v`
Expected: FAIL — `reaped_sessions` is empty / `reaps_without_mapping` fails because the pool never calls the backend hook.

- [ ] **Step 3: Update `SandboxPool.destroy_for_session`**

In `surogates/sandbox/pool.py`, replace the `destroy_for_session` method body:

```python
    async def destroy_for_session(self, session_id: str) -> None:
        """Destroy the sandbox for *session_id* and remove the mapping.

        Also calls the backend's optional ``destroy_for_session`` (Docker)
        so stale containers/pods labelled for this session are reaped even
        when this pool has no in-memory mapping (e.g. after a worker
        restart).
        """
        lock = await self._session_lock(session_id)
        async with lock:
            sandbox_id = self._mapping.pop(session_id, None)
            self._specs.pop(session_id, None)
            if sandbox_id is not None:
                await self._backend.destroy(sandbox_id)
                logger.info(
                    "Destroyed sandbox %s for session %s",
                    sandbox_id,
                    session_id,
                )

            # Optional backend-level reap (label-based), independent of the
            # mapping above.
            backend_reap = getattr(self._backend, "destroy_for_session", None)
            if backend_reap is not None:
                await backend_reap(session_id)

        # Clean up the per-session lock to prevent unbounded growth.
        async with self._global_lock:
            self._locks.pop(session_id, None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_sandbox.py -k destroy_for_session -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add surogates/sandbox/pool.py tests/test_sandbox.py
git commit -m "feat: SandboxPool reaps via optional backend destroy_for_session"
```

---

### Task 9: Spec builder sets `session_id` and `workspace_path`

**Files:**
- Modify: `surogates/harness/tool_exec.py` (`_build_session_sandbox_spec`, ~lines 61-123)
- Test: `tests/test_tool_exec_sandbox_spec.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tool_exec_sandbox_spec.py`:

```python
def test_spec_sets_session_id_and_workspace_path():
    session = _session(
        config={
            "storage_bucket": "agent-bucket",
            "workspace_path": "/data/agent-bucket/sessions/root-1",
        },
    )

    spec = _build_session_sandbox_spec(
        session, tenant=SimpleNamespace(), sandbox_owner="root-1",
    )

    assert spec.session_id == "root-1"
    assert spec.workspace_path == "/data/agent-bucket/sessions/root-1"


def test_spec_workspace_path_none_when_absent():
    session = _session(config={"storage_bucket": "agent-bucket"})

    spec = _build_session_sandbox_spec(
        session, tenant=SimpleNamespace(), sandbox_owner="root-1",
    )

    assert spec.session_id == "root-1"
    assert spec.workspace_path is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_tool_exec_sandbox_spec.py -k "session_id_and_workspace or workspace_path_none" -v`
Expected: FAIL — `spec.session_id` is `""` and `spec.workspace_path` is `None` even when config has a path.

- [ ] **Step 3: Set the fields in `_build_session_sandbox_spec`**

In `surogates/harness/tool_exec.py`, just before the final `return sandbox_spec` in `_build_session_sandbox_spec`, add:

```python
    # Docker backend needs the root session key (for labels + stale-container
    # cleanup) and a host-bindable workspace path. K8sSandbox ignores both.
    # Delegation children inherit the root's workspace_path via
    # create_child_session, so reading session.config here is correct for them.
    sandbox_spec.session_id = sandbox_owner
    sandbox_spec.workspace_path = session.config.get("workspace_path")
    return sandbox_spec
```

(Remove the existing bare `return sandbox_spec` line that this replaces.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_tool_exec_sandbox_spec.py -v`
Expected: PASS (new tests + existing tests unchanged — the baseline-not-mutated tests still hold because `session_id`/`workspace_path` are set on the per-session copy, never on the tenant baseline).

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/tool_exec.py tests/test_tool_exec_sandbox_spec.py
git commit -m "feat: populate sandbox spec session_id and workspace_path"
```

---

### Task 10: Worker wires the `docker` backend branch

**Files:**
- Modify: `surogates/orchestrator/worker.py:640-656`
- Test: import-level construction check (worker startup is integration-only)

- [ ] **Step 1: Update the backend selection**

In `surogates/orchestrator/worker.py`, replace the sandbox-backend `if/else` (currently lines ~641-655):

```python
    if settings.sandbox.backend == "kubernetes":
        from surogates.sandbox.kubernetes import K8sSandbox

        sandbox_backend = K8sSandbox(
            namespace=settings.sandbox.k8s_namespace,
            service_account=settings.sandbox.k8s_service_account,
            pod_ready_timeout=settings.sandbox.k8s_pod_ready_timeout,
            executor_port=settings.sandbox.k8s_executor_port,
            storage_settings=settings.storage,
            s3fs_image=settings.sandbox.k8s_s3fs_image,
            s3_endpoint=settings.sandbox.k8s_s3_endpoint,
            mcp_proxy_url=settings.mcp_proxy_url,
        )
    else:
        sandbox_backend = ProcessSandbox()
    sandbox_pool = SandboxPool(sandbox_backend)
```

with:

```python
    if settings.sandbox.backend == "kubernetes":
        from surogates.sandbox.kubernetes import K8sSandbox

        sandbox_backend = K8sSandbox(
            namespace=settings.sandbox.k8s_namespace,
            service_account=settings.sandbox.k8s_service_account,
            pod_ready_timeout=settings.sandbox.k8s_pod_ready_timeout,
            executor_port=settings.sandbox.k8s_executor_port,
            storage_settings=settings.storage,
            s3fs_image=settings.sandbox.k8s_s3fs_image,
            s3_endpoint=settings.sandbox.k8s_s3_endpoint,
            mcp_proxy_url=settings.mcp_proxy_url,
        )
    elif settings.sandbox.backend == "docker":
        from surogates.sandbox.docker import DockerSandbox

        sandbox_backend = DockerSandbox(
            image=settings.sandbox.docker_image,
            executor_port_base=settings.sandbox.docker_executor_port_base,
            ready_timeout=settings.sandbox.docker_ready_timeout,
            network=settings.sandbox.docker_network,
            mcp_proxy_url=settings.mcp_proxy_url,
        )
    else:
        sandbox_backend = ProcessSandbox()
    sandbox_pool = SandboxPool(sandbox_backend)
```

- [ ] **Step 2: Verify the worker module imports and the branch constructs**

Run:

```bash
python -c "
from surogates.sandbox.docker import DockerSandbox
b = DockerSandbox(image='x', executor_port_base=33000, ready_timeout=5, network='bridge', mcp_proxy_url='')
print('DockerSandbox constructs:', type(b).__name__)
import surogates.orchestrator.worker as w
print('worker imports OK')
"
```

Expected: prints `DockerSandbox constructs: DockerSandbox` and `worker imports OK` with no traceback.

- [ ] **Step 3: Run the sandbox test suite to confirm nothing regressed**

Run: `pytest tests/test_docker_sandbox.py tests/test_k8s_sandbox.py tests/test_sandbox.py tests/test_executor_http_client.py tests/test_executor_server.py tests/test_tool_exec_sandbox_spec.py -v`
Expected: PASS (all).

- [ ] **Step 4: Commit**

```bash
git add surogates/orchestrator/worker.py
git commit -m "feat: select DockerSandbox when sandbox backend is docker"
```

---

### Task 11: Opt-in integration smoke test (real Docker)

**Files:**
- Create: `tests/integration/test_docker_sandbox_e2e.py`

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_docker_sandbox_e2e.py`:

```python
"""Opt-in end-to-end test for DockerSandbox against a real Docker daemon.

Skipped unless Docker is available AND SUROGATES_TEST_DOCKER_SANDBOX=1 is set,
so CI without a daemon (or without the sandbox image pulled) stays green.

Run locally with:
    SUROGATES_TEST_DOCKER_SANDBOX=1 pytest tests/integration/test_docker_sandbox_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from surogates.sandbox.base import SandboxSpec, SandboxStatus
from surogates.sandbox.docker import DockerSandbox

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or os.environ.get("SUROGATES_TEST_DOCKER_SANDBOX") != "1",
    reason="requires Docker and SUROGATES_TEST_DOCKER_SANDBOX=1",
)

_IMAGE = os.environ.get(
    "SUROGATES_TEST_SANDBOX_IMAGE",
    "ghcr.io/invergent-ai/surogates-agent-sandbox:latest",
)


async def test_provision_execute_workspace_destroy(tmp_path):
    backend = DockerSandbox(image=_IMAGE, executor_port_base=34000, ready_timeout=120)
    sid = None
    try:
        spec = SandboxSpec(
            session_id="00000000-0000-0000-0000-0000000000ee",
            workspace_path=str(tmp_path),
            timeout=60,
        )
        sid = await backend.provision(spec)
        assert await backend.status(sid) == SandboxStatus.RUNNING

        # Write a file through the terminal tool into the bind-mounted workspace.
        result = json.loads(await backend.execute(
            sid, "terminal", json.dumps({"command": "echo hello > /workspace/out.txt"}),
        ))
        assert result.get("exit_code", 0) == 0

        # The file is visible on the host bind-mount.
        assert (tmp_path / "out.txt").read_text().strip() == "hello"
    finally:
        if sid is not None:
            await backend.destroy(sid)
        await backend.aclose()
```

- [ ] **Step 2: Verify the test is collected and skipped without the opt-in**

Run: `pytest tests/integration/test_docker_sandbox_e2e.py -v`
Expected: `1 skipped` (reason: requires Docker and `SUROGATES_TEST_DOCKER_SANDBOX=1`).

- [ ] **Step 3: (Optional, local only) Run for real**

If a Docker daemon is available and the sandbox image is built/pulled:

```bash
SUROGATES_TEST_DOCKER_SANDBOX=1 pytest tests/integration/test_docker_sandbox_e2e.py -v
```
Expected: PASS. (Skip this step in environments without Docker.)

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_docker_sandbox_e2e.py
git commit -m "test: add opt-in DockerSandbox e2e smoke test"
```

---

## Self-Review

**Spec coverage** — every spec section maps to a task:
- `ExecutorHTTPClient` (spec §Architecture) → Task 1.
- `K8sSandbox` refactor (spec §Refactored) → Task 2.
- `SandboxSpec.session_id`/`workspace_path` (spec §Sandbox spec and workspace) → Task 3; populated in Task 9.
- `TOOL_EXECUTOR_REQUIRE_FUSE` (spec §Readiness) → Task 4.
- Config `backend` literal + docker fields (spec §Configuration) → Task 5.
- `DockerSandbox` provision/execute/status/destroy/destroy_for_session/readiness/workspace (spec §Architecture, §Workspace) → Task 6.
- MCP proxy token + KB env + host URL rewrite (spec §Networking) → Task 7.
- `SandboxPool.destroy_for_session` optional hook (spec §Architecture bullet) → Task 8.
- Worker `docker` branch + `__init__` export (spec §Configuration) → Task 10 / Task 6 Step 4.
- Tests (spec §Testing): `ExecutorHTTPClient` T1, K8s regression T2, `DockerSandbox` T6+T7, spec builder T9, executor_server healthz T4, integration T11. All covered.

**Placeholder scan** — no "TBD"/"handle edge cases"/"similar to". Task 9 now uses the concrete `_session` helper and `SimpleNamespace` already present in `tests/test_tool_exec_sandbox_spec.py`.

**Type/name consistency** — checked across tasks: `ExecutorHTTPClient.execute(*, host, port, token, name, args_str, timeout)` is defined in T1 and called identically in T2 (K8s) and T6 (Docker). `_Entry(sandbox_id, container_id, host_port, token, spec, status)` defined in T6 is constructed with the same fields in T6 tests. `_build_env(spec, sandbox_id, token)` signature is stable from T6 to T7. `create_app(..., require_fuse=...)` defined T4, used in T4 tests. `SandboxSpec.session_id`/`workspace_path` defined T3, set T9, read T6/T7. `destroy_for_session(session_id)` is the method name in T6 (backend), T8 (pool delegates), and T8 fake backends.

## Execution Handoff

(Offered to the user after this plan is saved.)
