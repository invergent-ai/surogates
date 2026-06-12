# Persistent Sandbox Tool-Executor — Design

**Date:** 2026-06-12
**Status:** Approved
**Repos touched:** `surogates` (sandbox backend, pool, sandbox image)

## Problem

PROD session `3fa7bb45-4bd4-4e4a-90f5-d755739283c7` ("it hanged immediately")
did not hang — it completed, but every sandbox tool call took 5–35 s, so a
filesystem-heavy turn ran for ~3 minutes with long silent gaps. The cause is
systemic, confirmed across 6 h of PROD traffic:

| Tool         | p50    | p90    |   | In-process tool   | p50    |
|--------------|--------|--------|---|-------------------|--------|
| search_files | 12.4 s | 34.8 s |   | web_search        | 1.1 s  |
| list_files   | 8.1 s  | 22.9 s |   | create_artifact   | 0.7 s  |
| read_file    | 7.5 s  | 24.7 s |   | kb_read_page      | 9 ms   |
| write_file   | 5.6 s  | 8.1 s  |   | todo              | 0 ms   |
| terminal     | 5.4 s  | 9.3 s  |   |                   |        |

### Latency stack (measured, in order of impact)

1. **Python cold start per tool call (~2–4 s).** Every call K8s-execs
   `images/sandbox/tool-executor`, a Python script that imports
   `surogates.tools.registry` + `ToolRuntime` from scratch. The import costs
   ~7.5 CPU-seconds; sandbox pods are capped at cpu 2 (request) / 4 (limit).
   This is the ~5 s floor visible on `terminal` and `write_file`.
2. **Per-session lock serializes every sandbox tool (~N×).**
   `SandboxPool.execute()` holds the session lock across the whole exec
   (`surogates/sandbox/pool.py:108-120`). The harness's parallel dispatch
   (`execute_tool_calls_concurrent`, `MAX_TOOL_WORKERS=8`, streaming overlap)
   is nullified for sandbox tools: a batch of 4 returned at 7.5 / 12.4 / 17.5 /
   22.9 s — cumulative, not concurrent.
3. **K8s exec WebSocket handshake (~0.3–0.5 s)** through the API server per
   call (`surogates/sandbox/kubernetes.py:176-190`).
4. **geesefs cold metadata (~0.2–2 s).** The workspace FUSE mount (geesefs,
   not s3fs; disk cache enabled) pays S3 LIST round-trips on cold
   readdir/glob. Warm operations are fine.
5. **Cold sandbox provisioning (~8 s, first tool of a session).** Pod create +
   geesefs mount; no warm pool exists for sandboxes.

Fixing only the FUSE layer would leave tools at ~5 s. This design removes
costs 1–3; 4 and 5 are explicitly out of scope (see Non-goals).

## Goals / success criteria

- Warm-sandbox `read_file` / `list_files` / `search_files` / `write_file`
  p50 **< 1 s** (from 5.5–12 s).
- `terminal` overhead < 300 ms + actual command runtime.
- A batch of N parallel-safe tools completes in ≈ max(individual), not sum.
- Byte-identical tool results — same handlers, same registry, only the
  dispatch path changes.

## Non-goals

- **geesefs tuning.** After this design, cold-glob cost is ~0.2–2 s worst
  case. Touching stat/list cache TTLs risks upload-visibility regressions
  (the UI writes `uploads/` directly to S3 mid-session). Follow-up if needed.
- **Warm sandbox pool** for the ~8 s provisioning latency. Separate project;
  this design makes it strictly easier (readiness = daemon up).
- **Orphaned `Failed` sandbox pods** in `surogates-sandboxes` (46 observed) —
  separate hygiene issue.
- `ProcessSandbox` (local/dev backend) — does not use tool-executor; untouched.

## Design

### 1. Executor daemon (sandbox side)

New module `surogates/sandbox/executor_server.py`, shipped inside the sandbox
image and run as the sandbox container's main process (replacing
`sleep infinity`). FastAPI + uvicorn — both are already core deps of the
`surogates` package, so the image gains no new dependencies.

- **Boot:** import `ToolRegistry` / `ToolRuntime`, `register_builtins()`
  once. The ~7.5 CPU-sec import happens during pod startup, hidden inside
  provisioning.
- **`POST /execute`** — body `{"name": str, "args": dict}`; runs
  `registry.dispatch(name, args, workspace_path=$WORKSPACE_DIR, tools=registry)`
  and returns the same JSON string the CLI printed on stdout today (raw body,
  `text/plain`). Concurrent requests execute concurrently on the event loop;
  a server-side `asyncio.Semaphore(8)` (matching `MAX_TOOL_WORKERS`) protects
  pod CPU. Per-request `asyncio.wait_for(timeout)` where the timeout comes
  from the request body (`"timeout"` key, defaulting to the spec default) —
  on expiry return the same `timed_out` result JSON the exec path produced.
- **`GET /healthz`** — 200 only when (a) the registry finished loading and
  (b) `/workspace` is a live mount (`os.path.ismount` or the
  `.s3fs-mounted` sentinel). 503 otherwise. Wired as the sandbox container's
  `readinessProbe` (httpGet, port 8071); `provision()`'s existing
  wait-for-Ready then automatically means "daemon and workspace ready".
- **Auth:** every `/execute` call requires
  `Authorization: Bearer $TOOL_EXECUTOR_TOKEN`; constant-time compare; 401
  otherwise. `/healthz` is unauthenticated (kubelet probes it).
- **Internal commands preserved verbatim:** `_checkpoint` (checkpoint
  manager) and `_code` (`pod_runner.dispatch`; its args are never logged —
  may carry a credential) keep their dedicated branches, identical to the
  current CLI behaviour, including the no-arg-logging rule.
- **Shutdown:** SIGTERM → stop accepting, let in-flight requests finish
  within the pod's termination grace period.
- **CLI compatibility:** `images/sandbox/tool-executor` becomes a thin client
  that POSTs to `localhost:8071` using `TOOL_EXECUTOR_TOKEN` from its env.
  Keeps the `kubectl exec <pod> -- tool-executor <name> <json>` debugging
  affordance with exactly one dispatch path. The old in-CLI dispatch code is
  deleted, not kept as a fallback.

Port 8071 is a new `SandboxSettings` field (`k8s_executor_port`), default
8071.

### 2. Worker side

- **`KubernetesSandbox.execute()`** — replace the per-call K8s exec with an
  `aiohttp` POST to `http://<pod-ip>:8071/execute`. Pod IP is captured at
  provision time (after Ready) and stored on the pod entry alongside the
  token. One shared `aiohttp.ClientSession` per backend (connection pooling);
  client timeout = `entry.spec.timeout + 5` (same buffer as today).
  `_exec_in_pod` and the WS-exec machinery are **deleted** (no legacy
  fallback path).
- **Error classification** mirrors today's semantics. Handler exceptions are
  caught *inside the daemon* and returned as 200 + error-result JSON (exactly
  what the CLI printed on stdout); HTTP status codes only describe transport
  health:
  - connect-refused / connect-timeout / 401 → mark entry
    `SandboxStatus.FAILED`, raise `SandboxUnavailableError` (the harness
    already surfaces a single "sandbox unavailable" result and the next
    `ensure()` destroys + reprovisions);
  - read-timeout past the tool budget → worker synthesizes the `timed_out`
    result JSON (tool-level failure, sandbox stays healthy);
  - HTTP 500 (daemon bug, not handler bug) → worker synthesizes the
    error-result JSON.
- **`SandboxPool.execute()` lock fix** — acquire the session lock only to
  resolve `sandbox_id`, then call `backend.execute()` **outside** the lock.
  `ensure()` and `destroy_for_session()` keep their locking. Rationale: the
  harness already gates which tools may batch in parallel
  (`should_parallelize`, path-overlap checks, guardrail pre-pass); pool-level
  serialization was redundant. A destroy racing an in-flight call makes that
  call fail with `SandboxUnavailableError` — identical to a pod dying
  mid-exec today.

### 3. Security

- **Token:** `secrets.token_urlsafe(32)` generated per sandbox at provision,
  injected as `TOOL_EXECUTOR_TOKEN` into the pod env and kept on the worker's
  pod entry. Same exposure posture as the existing `MCP_PROXY_TOKEN`.
- **NetworkPolicy:** `surogates-sandboxes` currently has none, so 8071 would
  be reachable cluster-wide. Add a policy manifest (mirroring the
  browser-fleet one): ingress to sandbox pods on 8071 allowed only from
  `runtime-worker` pods in the `surogates` namespace; applied at deploy time
  alongside the existing infra manifests. Token auth is the primary control;
  the policy is hardening. When applying, verify kubelet readiness probes
  still reach 8071 under the cluster CNI (most CNIs exempt host/kubelet
  traffic from NetworkPolicy, but this must be confirmed, not assumed).
- **Local access inside the pod:** agent code (`terminal`) can hit
  localhost:8071 — no privilege escalation, since `terminal` already runs
  with the same user, env, and workspace as the tool handlers.

### 4. Rollout / version skew

Worker and sandbox image ship in the same release. During the deploy window a
new worker may hold a mapping to an old sandbox pod (no daemon): the HTTP
connect fails → entry marked `FAILED` → next `ensure()` reprovisions with the
new image. One failed tool call, then self-heal — this is the same path as
daemon-crash recovery, not a parallel legacy branch. (Worker restarts also
drop the in-memory pool mapping, so most sessions get fresh pods anyway.)

## Testing

- **Daemon unit tests** (FastAPI test client): 401 without/with-wrong token;
  happy-path dispatch returns handler JSON; per-request timeout returns
  `timed_out` JSON; `_checkpoint` and `_code` branches; N concurrent
  `/execute` calls actually overlap; `/healthz` 503 before registry
  load/mount, 200 after.
- **Worker unit tests:** `KubernetesSandbox.execute()` against a mock server —
  result passthrough, connect-refused → `FAILED` + `SandboxUnavailableError`,
  read-timeout → `timed_out` JSON, 500 → error JSON.
- **Pool unit test:** two concurrent `execute()` calls against a slow fake
  backend overlap in time (assert wall < sum of durations).
- **Integration (local k3d):** provision a real pod; batch of 4 filesystem
  tools completes in ≈ max not sum; warm `read_file` p50 < 1 s; kill the
  daemon process → next call fails unavailable → following call gets a fresh
  pod.
- **Regression:** `TOOL_LOCATIONS` routing tests unaffected; `_code`
  pod-runner flow through the daemon.

## Future work

- Warm sandbox pool (fleet-mode machinery already exists for browsers and the
  geesefs entrypoint already supports late-binding credentials).
- geesefs cache tuning with explicit upload-visibility tests.
- Reaper for orphaned `Failed` sandbox pods.
