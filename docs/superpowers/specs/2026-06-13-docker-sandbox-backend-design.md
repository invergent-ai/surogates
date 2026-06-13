# Docker Sandbox Backend — Design

**Date:** 2026-06-13
**Status:** Approved (pending spec review)
**Scope:** `surogates` framework — `surogates/sandbox/`

## Summary

Add a third sandbox backend, `DockerSandbox`, alongside the existing
`ProcessSandbox` (subprocess, dev-only, effectively outdated) and
`K8sSandbox` (production). It runs the existing agent-sandbox image as one
Docker container per root session and talks to the in-container tool-executor
daemon over HTTP — the **same contract** `K8sSandbox` uses — while managing the
container lifecycle the way the browser's `ProcessBrowserBackend` manages its
kernel-images container.

In one line: **`K8sSandbox`'s executor-daemon HTTP contract, delivered via
`ProcessBrowserBackend`'s `docker run` lifecycle.**

### Why

`ProcessSandbox` runs commands as raw subprocesses in a throwaway tempdir that
is disconnected from the storage backend. It is the local-dev backend today,
but it diverges from production: no container isolation, no `/workspace`-equals-
storage semantics, and a different execution model from the executor daemon.

`DockerSandbox` gives local development real per-session container isolation and
production-like `/workspace` semantics **without requiring a Kubernetes
cluster**, by reusing the exact image and daemon that production runs.

### Target use case

Local development on a trusted, single-user host. This is explicitly **not** a
multi-tenant production backend (that role is `K8sSandbox`). Hardening for a
single-node multi-tenant Docker deployment is out of scope; the structure does
not preclude it later.

## Background: how the existing backends work

The router (`surogates/tools/router.py`) dispatches every `SANDBOX`-located
tool call as `pool.execute(session_id, name, args_json)`, where `name` is a
**tool name** (`"terminal"`, `"write_file"`, `_checkpoint`, …) and the payload
is **JSON arguments**. This is the executor-daemon contract.

- **`K8sSandbox`** provisions one pod per session. The pod's main process is
  `surogates.sandbox.executor_server` (a FastAPI daemon that loads the tool
  registry once and forks a child per call). The worker `POST`s
  `{name, args, timeout}` to the pod IP with a per-sandbox bearer token.
- **`ProcessSandbox`** treats `name` as a raw executable and `input` as stdin.
  This does **not** match the router's tool-name contract for the full toolset;
  it is the legacy/dev path and is being superseded, not extended.
- **`ProcessBrowserBackend`** (`surogates/browser/process.py`) is the model for
  container lifecycle: `docker run -d --rm`, published ports with conflict
  retry, `--label surogates.session_id=…`, optional workspace bind-mount, an
  HTTP readiness poll, and `destroy_for_session` by label.

The sandbox image (`images/sandbox/Dockerfile`,
`ghcr.io/invergent-ai/surogates-agent-sandbox:latest`) bundles the full
`surogates` package plus productivity tooling, runs as the non-root `sandbox`
user, and its `ENTRYPOINT`/`CMD` is
`tini -- python -m surogates.sandbox.executor_server`. The container's main
process **is** the executor daemon, so `DockerSandbox` gets full tool parity for
free — it only has to run the image and speak HTTP to it.

## Architecture

### New: `surogates/sandbox/_executor_client.py` — `ExecutorHTTPClient`

A backend-agnostic helper that owns the worker→daemon HTTP transport. Extracted
verbatim from the logic currently inside `K8sSandbox`, then shared by both the
K8s and Docker backends.

Responsibilities:

- A lazily-created shared `aiohttp.ClientSession` (connection pooling across
  tool calls) and an `aclose()` to release it on worker shutdown.
- `async execute(*, host, port, token, name, args_str, timeout) -> str` — `POST`
  to `http://{host}:{port}/execute` with `{"name", "args", "timeout"}` and the
  `Authorization: Bearer {token}` header. The error taxonomy is preserved
  exactly from the current `K8sSandbox.execute`:
  - HTTP **401** → raise `SandboxUnavailableError` (token mismatch; sandbox is
    unusable and must be reprovisioned).
  - HTTP **non-200** → return an error `_result_json` (daemon reachable but
    erroring).
  - **`aiohttp.ClientConnectionError`** → raise `SandboxUnavailableError`.
    **This clause must be ordered before `TimeoutError`** because aiohttp's
    connect-phase timeouts inherit from both; they mean "daemon unreachable" and
    must not be misclassified as a tool timeout.
  - **`asyncio.TimeoutError`** (plain total-budget expiry) → return a
    `timed_out` `_result_json`.
- `@staticmethod _result_json(*, exit_code, stdout, stderr, truncated,
  timed_out) -> str` — the standard sandbox result shape. Identical in both
  backends today; this becomes the single definition.

**Entry-state bookkeeping stays in each backend.** The helper signals fatal
conditions by raising `SandboxUnavailableError` and never touches backend
objects. Each backend's thin `execute` wrapper catches that error, marks its own
entry `FAILED` (so the next `SandboxPool.ensure` reprovisions), and re-raises.

`args_str` parsing (the `json.loads(input) if input else {}` guard) lives in the
helper so both backends pass the router's raw JSON string through unchanged.

### New: `surogates/sandbox/docker.py` — `DockerSandbox`

Implements the `Sandbox` protocol (`provision` / `execute` / `destroy` /
`status`). Holds one `ExecutorHTTPClient`.

- **`_DockerDriver` / `_RealDocker`** — the same injectable subprocess driver
  pattern as `browser/process.py` (`async run(args) -> (code, stdout, stderr)`),
  so tests never touch a real Docker daemon.

- **`_Entry`** — per-sandbox bookkeeping: `container_id`, published `host_port`,
  `token`, `spec`, `status`.

- **`provision(spec)`**:
  1. Mint `executor_token = secrets.token_urlsafe(32)`.
  2. Allocate `host_port = docker_executor_port_base + offset`, retrying on
     port-conflict stderr up to a fixed cap (mirrors `ProcessBrowserBackend`).
     The daemon always binds a **fixed in-container port** (`8071`, the image
     default); only the published host port varies. The client connects to the
     host port.
  3. `docker run -d --rm` with:
     - `-p {host_port}:8071` (host port → fixed in-container daemon port)
     - `--label app=surogates-sandbox --label surogates.session_id={…}`
     - `--add-host=host.docker.internal:host-gateway` (host-service reachability
       for MCP/KB tools, best-effort)
     - `-v {workspace_host_path}:/workspace` when a host path is resolvable
       (see Workspace), otherwise no mount (ephemeral container `/workspace`)
     - env: `TOOL_EXECUTOR_TOKEN`, `WORKSPACE_DIR=/workspace`,
       `TOOL_EXECUTOR_REQUIRE_FUSE=0`, plus `spec.env` passthrough (with
       host-targeting URL rewrite, see Networking). `TOOL_EXECUTOR_PORT` is left
       at the image default (`8071`).
     - the image from `spec.image` or the configured `docker_image`
  4. Poll `GET /healthz` until 200 within `docker_ready_timeout` seconds (the
     browser backend's `_wait_ready` shape). On timeout, `stop`+`rm` the
     container and raise `SandboxUnavailableError(classification="docker")`.
  5. Record the entry (`container_id`, `host_port`, `token`, `spec`); return the
     `sandbox_id`.

- **`execute(sandbox_id, name, input)`** — resolve the entry, delegate to
  `ExecutorHTTPClient.execute(host="127.0.0.1", port=entry.host_port,
  token=entry.token, …)`. On `SandboxUnavailableError`, set `entry.status =
  FAILED` and re-raise.

- **`status(sandbox_id)`** — `docker inspect --format {{.State.Status}}`, mapped
  to `SandboxStatus` exactly as the browser backend does
  (`running`→RUNNING, `created`/`restarting`→PENDING,
  `exited`/`dead`/`removing`→TERMINATED, else FAILED). Unknown id → TERMINATED.

- **`destroy(sandbox_id)`** — `docker stop` + `docker rm`, drop the entry.

- **`destroy_for_session(session_id)`** — `docker ps -aq --filter
  label=surogates.session_id={…}` then stop/rm each, covering containers not in
  the in-memory map (e.g. after a worker restart).

- **`aclose()`** — close the `ExecutorHTTPClient`. (`worker.py` shutdown already
  `getattr`-guards `aclose`, so this is picked up automatically.)

### Refactored: `surogates/sandbox/kubernetes.py` — `K8sSandbox`

Behavior-preserving change only: delete the inline `_get_http`, `aclose`,
`execute` HTTP block, and `_result_json`, and delegate to an
`ExecutorHTTPClient` instance (calling it with `host=entry.pod_ip,
port=self._executor_port, token=entry.token`). The 401/connection-error entry
`FAILED` marking moves into the thin wrapper, identical to today. This is the
**only** edit to the production execution path; it is covered by a regression
test asserting identical result shapes.

## Workspace handling

The `SandboxPool` keys by **root** session (`sandbox_session_key`), so all
delegation children and loop ticks resolve to one `sandbox_id` → one container →
one `/workspace`. Sharing is handled by the pool; the backend only decides what
`/workspace` is backed by.

- **Local dev (`LocalBackend`, the default):** bind-mount the root session's
  on-disk workspace directory
  (`LocalBackend.resolve_workspace_path` → `{base_path}/{bucket}/sessions/{root}`)
  as `/workspace`. This makes `/workspace == storage` (matching K8s s3fs
  semantics), survives a mid-session container reprovision, and is inspectable
  on the host.
- **No resolvable host path (e.g. `S3Backend` with no s3fs sidecar):** run with
  an ephemeral container-internal `/workspace`. Local Docker mode does not run
  an s3fs sidecar.

**Plumbing.** Add `workspace_path: str | None = None` to `SandboxSpec` (mirrors
`BrowserSpec.workspace_path`). `_build_session_sandbox_spec` in
`surogates/harness/tool_exec.py` already computes the **root**'s workspace
prefix for the storage `Resource`; it populates `spec.workspace_path` with the
root's resolved host directory. `DockerSandbox` bind-mounts it when present.
`K8sSandbox` ignores the field (it mounts via the s3fs sidecar as before).

## Networking

Bridge networking with a published executor port, like the browser backend. The
container reaches host services via `host.docker.internal` (injected with
`--add-host=…:host-gateway`). Env vars that point at host-local services
(e.g. `MCP_PROXY_URL`, `SUROGATES_OPS_DB_URL`) are rewritten on a best-effort
basis: `localhost`/`127.0.0.1` → `host.docker.internal`. This mirrors the
existing k3d `host.k3d.internal` convention.

Host wiring is best-effort: terminal and file tools work regardless. MCP and KB
tools work when their host services are running locally; when they are not, only
those tools fail (the sandbox itself stays healthy).

## Readiness / `executor_server` change

`executor_server`'s `/healthz` returns 200 only when `workspace_mounted()` finds
a **FUSE** mount at `/workspace` (the s3fs readiness signal). A bind-mount is not
FUSE, and an ephemeral workspace has no mount at all, so under Docker `/healthz`
would return 503 forever and `DockerSandbox` would never see the container as
ready.

Add a `TOOL_EXECUTOR_REQUIRE_FUSE` env var, **default `"1"` (preserves K8s
behavior exactly)**. When `"0"`, `/healthz` skips the FUSE check and returns 200
once the tool registry has loaded. `DockerSandbox` sets it to `"0"`. ~5 lines,
additive, no effect on the K8s path.

## Configuration

`surogates/config.py`, `SandboxSettings` (`env_prefix = SUROGATES_SANDBOX_`):

- `backend: Literal["process", "kubernetes", "docker"] = "process"` (add
  `"docker"`).
- `docker_image: str = "ghcr.io/invergent-ai/surogates-agent-sandbox:latest"`
- `docker_executor_port_base: int = 33000` (chosen to avoid the browser
  backend's 30000/31000/32000 bases).
- `docker_ready_timeout: int = 60` (seconds to wait for `/healthz`; matches the
  `k8s_pod_ready_timeout` default).
- `docker_network: str = "bridge"`

`surogates/orchestrator/worker.py` — add a `docker` branch in the backend
selection that constructs `DockerSandbox(image=…, executor_port_base=…,
network=…, storage_backend=storage_backend)`. The storage backend is passed so
the workspace host-path is resolvable.

`surogates/sandbox/__init__.py` — export `DockerSandbox`.

## Data flow (one tool call)

```
router (SANDBOX location)
  → SandboxPool.ensure(root_id, spec)          # provisions on first call, then cached
      → DockerSandbox.provision(spec)          # docker run + /healthz poll
  → SandboxPool.execute(root_id, "terminal", args_json)
      → DockerSandbox.execute(...)
          → ExecutorHTTPClient.execute(host=127.0.0.1, port=host_port, token, ...)
              → POST 127.0.0.1:{host_port}/execute
                  → daemon forks child → runs tool → result JSON
          ← result JSON (unchanged shape)
```

## Error handling

- `docker run` failure (missing image, daemon down, no free port after retries):
  `SandboxUnavailableError(classification="docker")`.
- Daemon unreachable / 401 at execute time: `SandboxUnavailableError` from the
  shared client → entry marked `FAILED` → `SandboxPool.ensure` reprovisions on
  the next call. With the bind-mount, reprovision preserves session files.
- Tool-level timeout: standard `timed_out` result JSON (no exception).

This reuses the existing `SandboxUnavailableError` contract end to end, so the
harness's "stop dispatching sandbox tools" behavior works identically to K8s.

## Testing

- **`ExecutorHTTPClient`** (`tests/test_executor_http_client.py`): inject a fake
  aiohttp transport; assert 200 / 401 / non-200 / `ClientConnectionError` /
  `TimeoutError` each map to the right result-or-raise. Assert `ClientConnection`
  takes precedence over `TimeoutError`.
- **`K8sSandbox` regression** (extend existing K8s tests): same inputs produce
  byte-identical results after delegating to the shared client; 401/connection
  errors still mark the entry `FAILED`.
- **`DockerSandbox`** (`tests/test_docker_sandbox.py`): inject a fake
  `_DockerDriver`; assert `provision` arg construction (port mapping, labels,
  bind-mount when `workspace_path` set / omitted when not, env including
  `TOOL_EXECUTOR_REQUIRE_FUSE=0` and `--add-host`), port-conflict retry, status
  mapping, `destroy`, and `destroy_for_session` label filtering. Mirrors
  `tests/test_browser_process.py`.
- **`executor_server`** (extend existing tests): `/healthz` → 200 with
  `TOOL_EXECUTOR_REQUIRE_FUSE=0` and no FUSE mount; still 503 by default
  (FUSE expected, absent).
- **Integration** (`tests/integration/test_docker_sandbox_e2e.py`, opt-in,
  skipped without a Docker daemon): real `docker run` of the sandbox image,
  execute `terminal` + a file write, verify the bind-mounted workspace on the
  host, then `destroy`. Mirrors `test_browser_e2e`.

## Files touched

**New**
- `surogates/sandbox/_executor_client.py`
- `surogates/sandbox/docker.py`
- `tests/test_executor_http_client.py`
- `tests/test_docker_sandbox.py`
- `tests/integration/test_docker_sandbox_e2e.py`

**Modified**
- `surogates/sandbox/kubernetes.py` (delegate to shared client)
- `surogates/sandbox/base.py` (`SandboxSpec.workspace_path`)
- `surogates/sandbox/executor_server.py` (`TOOL_EXECUTOR_REQUIRE_FUSE`)
- `surogates/sandbox/__init__.py` (export `DockerSandbox`)
- `surogates/config.py` (`SandboxSettings`: `backend` literal + docker fields)
- `surogates/orchestrator/worker.py` (`docker` backend branch)
- `surogates/harness/tool_exec.py` (populate `spec.workspace_path` from the root)

## Out of scope

- Multi-tenant hardening (seccomp/AppArmor profiles, network egress policy, user
  namespaces, cgroup enforcement beyond Docker defaults).
- An s3fs sidecar container for Docker mode (`S3Backend` under Docker falls back
  to ephemeral `/workspace`).
- Replacing or removing `ProcessSandbox` (left as-is).
- A docker-compose / single-node production deployment topology.
