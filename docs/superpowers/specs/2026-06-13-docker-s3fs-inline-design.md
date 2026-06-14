# R2-Backed Workspaces for Docker Sandbox Mode (Inline geesefs) — Design

**Date:** 2026-06-13
**Status:** Approved (pending spec review)
**Scope:** `surogates` framework — `surogates/sandbox/docker.py`, `images/sandbox/`
**Builds on:** `2026-06-13-docker-sandbox-backend-design.md` (the DockerSandbox backend)

## Summary

Give the Docker sandbox backend a third workspace mode — **`s3fs`** — that
mounts the session's R2 prefix at `/workspace` using **geesefs run inside the
sandbox container's entrypoint**. Writes then land in R2 at the same
`{key_prefix}/sessions/{root}` layout the ops server reads, so docker-mode
workspace files appear in the **Studio UI** (and any R2-backed pipeline),
matching Kubernetes behavior — without a separate sidecar container.

### Why

The ops server serves the Studio UI from **R2** (`~/.surogate/config.yaml`
`workspaces:` block). Docker mode currently never writes to R2: with
`storage=s3` the container `/workspace` is ephemeral, and with `storage=local`
it is a host bind-mount on disk — neither reaches R2. So docker-mode sessions
run correctly but their files are invisible in the Studio UI. In Kubernetes
this works only because an **s3fs/geesefs sidecar** FUSE-mounts R2 at
`/workspace`. This design reproduces that mount for docker mode, inline in the
sandbox container.

### Target use case

Local development on a trusted, single-user, **rootful** Docker host (verified:
`/dev/fuse` present, grantable to a uid-1000 container with
`--cap-add SYS_ADMIN --device /dev/fuse --security-opt apparmor:unconfined`).
Rootless Docker is out of scope (it cannot grant `/dev/fuse` + `SYS_ADMIN`);
such hosts keep using `bind`/`ephemeral` mode or Kubernetes.

## Background: how the K8s s3fs mount works

The K8s sandbox pod runs the `ghcr.io/invergent-ai/surogates-s3fs` image as a
sidecar. Its `entrypoint.sh` runs **geesefs** (a maintained goofys fork):

```
geesefs --endpoint $S3_ENDPOINT --region $S3_REGION \
        -o allow_other --uid 1000 --gid 1000 \
        --file-mode 0644 --dir-mode 0755 --memory-limit 256 \
        -f  <bucket>:<prefix>  /workspace
```

- It needs `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET_PATH`
  (`bucket` or `bucket:/prefix`), `S3_ENDPOINT`, optional `S3_REGION`.
- geesefs is a single static binary (pinned `GEESEFS_VERSION=v0.43.7`,
  `geesefs-linux-amd64`) plus the `fuse` package and `user_allow_other` in
  `/etc/fuse.conf` (for `-o allow_other`).
- `--uid 1000 --gid 1000 -o allow_other` makes the mount readable/writable by
  the sandbox container's non-root `sandbox` user (uid 1000).
- The bucket/prefix comes from the spec's `s3://bucket/{key_prefix}/sessions/{root}`
  workspace Resource; K8s parses it into `bucket:/prefix` (kubernetes.py:279-287).

## Architecture: three workspace modes

`DockerSandbox` selects the mode per provision; **no new config flag** — it is
derived from the spec + storage settings:

| Mode | When | `/workspace` backing | `docker run` extras |
|------|------|----------------------|---------------------|
| **`s3fs`** (new) | spec has an `s3://…` workspace Resource **and** storage creds are present | geesefs FUSE mount of the R2 prefix, inside the container | `--cap-add SYS_ADMIN --device /dev/fuse --security-opt apparmor:unconfined`; s3fs env; **no `-v`**; `REQUIRE_FUSE=1` |
| **`bind`** (existing) | a real host `workspace_path` resolves | host bind-mount | `-v {host}:/workspace`; `REQUIRE_FUSE=0` |
| **`ephemeral`** (existing) | neither | container-internal dir | `REQUIRE_FUSE=0` |

In `s3fs` mode `/workspace` is a genuine FUSE mount, so the daemon's existing
FUSE readiness check (`TOOL_EXECUTOR_REQUIRE_FUSE=1`, the default) passes
naturally — more parity with prod, and no reliance on the `=0` escape hatch.

## Image changes (`images/sandbox/`)

### `images/sandbox/Dockerfile`
- Add to the apt layer: `fuse`, and `echo "user_allow_other" >> /etc/fuse.conf`.
- Add the geesefs binary (self-contained; **curl the pinned release**, matching
  the s3fs image's `GEESEFS_VERSION`):
  ```dockerfile
  ARG GEESEFS_VERSION=v0.43.7
  RUN curl -fsSL -o /usr/local/bin/geesefs \
        "https://github.com/yandex-cloud/geesefs/releases/download/${GEESEFS_VERSION}/geesefs-linux-amd64" \
      && chmod +x /usr/local/bin/geesefs
  ```
- Replace the final `CMD` with a wrapper, keeping `tini` as the entrypoint:
  ```dockerfile
  COPY images/sandbox/entrypoint.sh /usr/local/bin/sandbox-entrypoint
  RUN chmod +x /usr/local/bin/sandbox-entrypoint
  ENTRYPOINT ["tini", "--"]
  CMD ["/usr/local/bin/sandbox-entrypoint"]
  ```

### `images/sandbox/entrypoint.sh` (new)
Conditional, gated by `SANDBOX_S3FS_INLINE`. When **off** (bind, ephemeral) it
is byte-for-byte equivalent to today's behavior (`exec` the daemon), so the
shared image stays prod-safe. (K8s is doubly safe: its pod manifest **overrides
the container command** — `command=["tini","--","python","-m",
"surogates.sandbox.executor_server"]`, kubernetes.py:418-420 — so it never runs
this wrapper at all; the sidecar still does the mounting there.) When **on**, it mounts geesefs,
waits for the mountpoint, **supervises** the daemon (does not `exec`, so it can
forward SIGTERM and unmount on shutdown — mirroring the s3fs image's fleet-mode
supervision so geesefs flushes writes before the container dies):

```bash
#!/bin/bash
set -euo pipefail
WORKSPACE="${WORKSPACE_DIR:-/workspace}"

if [ "${SANDBOX_S3FS_INLINE:-0}" != "1" ]; then
    exec python -m surogates.sandbox.executor_server
fi

: "${S3_BUCKET_PATH:?required for inline s3fs}"
: "${S3_ENDPOINT:?required for inline s3fs}"
bucket_spec="${S3_BUCKET_PATH/:\//:}"   # bucket:/prefix -> bucket:prefix

geesefs --endpoint "$S3_ENDPOINT" --region "${S3_REGION:-auto}" \
        -o allow_other --uid 1000 --gid 1000 \
        --file-mode 0644 --dir-mode 0755 \
        --memory-limit "${GEESEFS_MEMORY_LIMIT_MB:-256}" \
        -f "$bucket_spec" "$WORKSPACE" &
gpid=$!
for _ in $(seq 1 150); do            # up to ~30s
    mountpoint -q "$WORKSPACE" && break
    kill -0 "$gpid" 2>/dev/null || { echo "geesefs exited before mount" >&2; wait "$gpid" || true; exit 1; }
    sleep 0.2
done
mountpoint -q "$WORKSPACE" || { echo "geesefs mount timed out" >&2; exit 1; }

python -m surogates.sandbox.executor_server &
dpid=$!
trap 'kill -TERM "$dpid" 2>/dev/null || true; wait "$dpid" 2>/dev/null || true; \
      fusermount -u "$WORKSPACE" 2>/dev/null || true; \
      kill -TERM "$gpid" 2>/dev/null || true; exit 0' TERM INT
wait "$dpid"
fusermount -u "$WORKSPACE" 2>/dev/null || true
kill -TERM "$gpid" 2>/dev/null || true
```

## DockerSandbox changes (`surogates/sandbox/docker.py`)

- **Constructor:** add `storage_settings: Any = None` (endpoint/access_key/
  secret_key/region). The worker passes `settings.storage`, exactly as it does
  for `K8sSandbox`.
- **`_workspace_mode(spec) -> tuple[str, str | None]`** returns
  `("s3fs", bucket_spec)`, `("bind", host_path)`, or `("ephemeral", None)`:
  - `s3fs` when a spec Resource has `mount_path == "/workspace"` and a
    `source_ref` starting with `s3://`, **and** `storage_settings` has both
    `access_key` and `secret_key`. `bucket_spec` is parsed from the
    `source_ref` exactly as `K8sSandbox._build_pod_manifest` does
    (`s3://bucket/prefix` → `bucket:/prefix`; bare bucket → `bucket`).
  - `bind` when `_mountable_workspace(spec.workspace_path)` returns a path.
  - `ephemeral` otherwise.
- **`provision`** branches on the mode when building `docker run` args and env:
  - `s3fs`: append `--cap-add SYS_ADMIN`, `--device /dev/fuse`,
    `--security-opt apparmor:unconfined`; env `SANDBOX_S3FS_INLINE=1`,
    `S3_BUCKET_PATH={bucket_spec}`, `S3_ENDPOINT={storage.endpoint}`,
    `S3_REGION={storage.region or "auto"}`, `AWS_ACCESS_KEY_ID`,
    `AWS_SECRET_ACCESS_KEY`; **omit** `TOOL_EXECUTOR_REQUIRE_FUSE=0` (leave the
    daemon default of `1`); no `-v`. The R2 endpoint is public, so it is **not**
    run through `_rewrite_host_for_container`.
  - `bind`: `-v {host}:/workspace` + `TOOL_EXECUTOR_REQUIRE_FUSE=0` (today).
  - `ephemeral`: `TOOL_EXECUTOR_REQUIRE_FUSE=0` (today).
- **Env construction** moves the `TOOL_EXECUTOR_REQUIRE_FUSE` decision into the
  per-mode branch; MCP/KB wiring is unchanged.
- Readiness, status, destroy, reap, and the shared `ExecutorHTTPClient` are
  unchanged. On container teardown, the in-container FUSE mount dies with the
  container; the entrypoint's trap additionally flushes/unmounts on SIGTERM.

## Config / worker wiring

- `config.dev.yaml`: `storage.backend: s3` (R2) — reverts the temporary `local`
  switch. The `bind`/local mode remains available for FUSE-less dev.
- `surogates/orchestrator/worker.py`: pass `storage_settings=settings.storage`
  to `DockerSandbox(...)` (one added kwarg).

## Data flow (`s3fs` mode, one session)

```
router → SandboxPool.ensure(root, spec)
  → DockerSandbox.provision(spec)            # mode=s3fs, bucket_spec parsed
      → docker run -d --rm --cap-add SYS_ADMIN --device /dev/fuse \
           -e SANDBOX_S3FS_INLINE=1 -e S3_BUCKET_PATH=… -e S3_ENDPOINT=… \
           -e AWS_ACCESS_KEY_ID=… -e AWS_SECRET_ACCESS_KEY=… <image>
        → entrypoint: geesefs mounts bucket:prefix at /workspace (R2)
        → wait mountpoint → run executor daemon
      → /healthz (FUSE check, REQUIRE_FUSE=1) → 200 once mounted → ready
  → tool writes /workspace/… → geesefs → R2 {key_prefix}/sessions/{root}/…
  → ops server reads R2 → Studio UI shows the files
```

## Error handling

- **geesefs mount failure** (bad creds, endpoint unreachable, bucket missing):
  entrypoint exits non-zero → container exits → `_wait_ready` times out →
  `SandboxUnavailableError` (with the improved "last status / exception" detail);
  `docker logs <container>` shows geesefs's stderr.
- **Host refuses FUSE caps**: `docker run` fails → `SandboxUnavailableError(
  classification="docker")` carrying docker's stderr.
- **Mode mis-detect** (s3:// resource but no creds): falls back to `ephemeral`
  rather than attempting a doomed mount; logged at provision.

## Testing

- **`DockerSandbox` unit** (`tests/test_docker_sandbox.py`, fake docker driver):
  - `s3fs` mode: with `storage_settings` (creds) + an `s3://bucket/p/sessions/r`
    workspace Resource, assert run args include the three FUSE flags,
    `SANDBOX_S3FS_INLINE=1`, `S3_BUCKET_PATH=bucket:/p/sessions/r`,
    `S3_ENDPOINT`, `AWS_ACCESS_KEY_ID/SECRET`, **no** `-v`, and **no**
    `TOOL_EXECUTOR_REQUIRE_FUSE=0`.
  - `bucket_spec` parsing: `s3://b/x/y` → `b:/x/y`; bare `s3://b` → `b`.
  - Mode selection: creds present + s3:// → `s3fs`; host path → `bind`;
    s3:// but no creds → `ephemeral`.
  - `bind`/`ephemeral` regressions: still emit `REQUIRE_FUSE=0`, no FUSE caps.
- **Entrypoint** (`tests/test_sandbox_entrypoint.py`, shell-level): with
  `SANDBOX_S3FS_INLINE` unset, the script's non-inline branch resolves to
  `exec python -m surogates.sandbox.executor_server` (assert via a dry-run/
  `geesefs`-stub on PATH that records its args; or a focused bats test). Keep
  this light — the heavy validation is the integration test.
- **Integration** (`tests/integration/test_docker_sandbox_s3fs_e2e.py`, opt-in,
  skipped unless Docker + `SUROGATES_TEST_DOCKER_S3FS=1` + R2 creds in env):
  provision in `s3fs` mode against a real (test) R2 bucket, write a file via the
  `write_file` tool, then verify the object exists in R2 via `aioboto3`/the
  storage backend, and is cleaned up on `destroy`. This is the real proof that
  writes reach R2.

## Files touched

**New**
- `images/sandbox/entrypoint.sh`
- `tests/test_sandbox_entrypoint.py`
- `tests/integration/test_docker_sandbox_s3fs_e2e.py`

**Modified**
- `images/sandbox/Dockerfile` (fuse + geesefs + wrapper CMD)
- `surogates/sandbox/docker.py` (`storage_settings`, `_workspace_mode`, per-mode
  run args/env)
- `surogates/orchestrator/worker.py` (pass `storage_settings` to `DockerSandbox`)
- `tests/test_docker_sandbox.py` (s3fs-mode coverage)
- `config.dev.yaml` (`storage.backend: s3`)

## Risks & validation-first plan

**Primary risk:** non-root (uid 1000) geesefs FUSE mount inside the container.
Prerequisites are verified on this host (rootful docker, `/dev/fuse` present and
visible to a uid-1000 container with the three caps). **The implementation
plan's first task is a throwaway spike**: build the image, `docker run` it with
the caps and real R2 env, and confirm geesefs mounts as uid 1000 and a written
file appears in R2 — *before* wiring the backend. **Fallback if non-root mount
fails:** mount geesefs as root in the entrypoint, then drop to uid 1000 for the
daemon via `gosu` (add `gosu` to the image); documented but not built unless the
spike requires it.

**Secondary:** geesefs adds ~20 MB to the sandbox image. Harmless for prod
(unused there; the K8s sidecar still does the mounting, and the inline branch is
gated off). The `fuse` package + `user_allow_other` are inert without the caps.

## Out of scope

- Rootless Docker support (cannot grant `/dev/fuse` + `SYS_ADMIN`).
- A separate s3fs **sidecar** container for docker mode (rejected in favor of
  inline; see approaches in this session's discussion).
- Tuning geesefs cache/perf for docker mode (defaults mirror the sidecar).
- Changing K8s behavior (the sidecar path is untouched).
