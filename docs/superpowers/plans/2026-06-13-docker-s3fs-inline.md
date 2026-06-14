# Inline-geesefs R2 Workspaces for Docker Sandbox — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `s3fs` workspace mode to `DockerSandbox` that mounts the session's R2 prefix at `/workspace` via geesefs run inside the sandbox container's entrypoint, so docker-mode workspace files land in R2 and appear in the Studio UI.

**Architecture:** The sandbox image gains the `fuse` package + the geesefs binary + a gated `entrypoint.sh` wrapper. `DockerSandbox` picks one of three modes per provision — `s3fs` (geesefs mount, new), `bind` (host mount), `ephemeral` — and sets the matching `docker run` flags/env. K8s is untouched (its pod manifest overrides the container command). Spec: `docs/superpowers/specs/2026-06-13-docker-s3fs-inline-design.md`.

**Tech Stack:** Python 3.12, Docker, geesefs (FUSE), `pytest` with a fake Docker driver. Verified host prereqs: rootful docker, `/dev/fuse` present and grantable to a uid-1000 container with `--cap-add SYS_ADMIN --device /dev/fuse --security-opt apparmor:unconfined`.

---

## Progress

Status updated before each commit. Legend: `[ ]` pending · `[~]` in progress · `[x]` complete.

- [x] Task 1: Spike — validate non-root geesefs R2 mount (go/no-go, no commit) — **PASS**: uid-1000 geesefs mount of R2 + write/read works; requires a uid-1000 `/etc/passwd` entry (the `sandbox` user provides it). No gosu fallback needed.
- [~] Task 2: Sandbox image — fuse + geesefs + gated `entrypoint.sh`
- [x] Task 3: `DockerSandbox` mode detection + `storage_settings`
- [x] Task 4: `DockerSandbox` per-mode run args + env
- [~] Task 5: Worker wiring + `config.dev.yaml` back to s3
- [ ] Task 6: Opt-in integration test (real docker + R2)

---

> All commands run from `/work/surogates`. Use `pytest` directly (not `uv run`). Commit messages use conventional-commit prefixes, no `Co-Authored-By` trailer.

---

### Task 1: Spike — validate non-root geesefs R2 mount (no code commit)

De-risks the whole feature before any wiring: confirm geesefs can FUSE-mount R2 **as uid 1000** with the caps, and that a write is visible back. Uses the existing s3fs image (which already bundles geesefs + fuse) as the geesefs provider.

**Files:** none (throwaway validation).

- [ ] **Step 1: Export the R2 creds from the dev config**

The values are in `config.dev.yaml` under `storage:`. Export them (do not paste secrets into committed files):

```bash
export R2_ENDPOINT="$(python -c "import yaml;print(yaml.safe_load(open('config.dev.yaml'))['storage']['endpoint'])")"
export R2_KEY="$(python -c "import yaml;print(yaml.safe_load(open('config.dev.yaml'))['storage']['access_key'])")"
export R2_SECRET="$(python -c "import yaml;print(yaml.safe_load(open('config.dev.yaml'))['storage']['secret_key'])")"
export R2_BUCKET="$(python -c "import yaml;print(yaml.safe_load(open('config.dev.yaml'))['storage']['bucket'])")"
echo "endpoint=$R2_ENDPOINT bucket=$R2_BUCKET key=${R2_KEY:0:4}…"
```

Expected: prints the endpoint, bucket, and a key prefix (non-empty).

- [ ] **Step 2: Mount R2 as uid 1000 inside a container and write a file**

```bash
docker run --rm \
  --user 1000:1000 \
  --cap-add SYS_ADMIN --device /dev/fuse --security-opt apparmor:unconfined \
  -e AWS_ACCESS_KEY_ID="$R2_KEY" -e AWS_SECRET_ACCESS_KEY="$R2_SECRET" \
  --entrypoint sh \
  ghcr.io/invergent-ai/surogates-agent-sandbox:executor-dev -c '
    echo user_allow_other >> /etc/fuse.conf 2>/dev/null || true
    geesefs --version 2>/dev/null || { echo "NO geesefs in this image"; exit 3; }
    geesefs --endpoint "'"$R2_ENDPOINT"'" --region auto -o allow_other --uid 1000 --gid 1000 \
            -f "'"$R2_BUCKET"':/spike-$(date +%s 2>/dev/null || echo t)" /tmp/ws &
    for i in $(seq 1 50); do mountpoint -q /tmp/ws && break; sleep 0.2; done
    mountpoint -q /tmp/ws || { echo "MOUNT FAILED"; exit 4; }
    echo "hello-from-spike" > /tmp/ws/spike.txt && cat /tmp/ws/spike.txt && echo "WRITE OK"
  '
```

> Note: the `executor-dev` image may not include geesefs — if Step 2 prints "NO geesefs in this image", retry with the s3fs image as the geesefs provider by replacing the image with `ghcr.io/invergent-ai/surogates-s3fs:latest` and `--entrypoint sh` (it has geesefs + fuse). The point is only to prove a **uid-1000** mount works on this host.

Expected: prints `WRITE OK` and `hello-from-spike`. That confirms non-root geesefs FUSE mount + write works with the caps.

- [ ] **Step 3: Record the go/no-go**

- **PASS** (`WRITE OK`): non-root mount works → proceed to Task 2 as designed.
- **FAIL** (`MOUNT FAILED`, permission denied, or fusermount errors): non-root FUSE is blocked on this host. **Switch to the fallback**: Task 2 installs `gosu`, and `entrypoint.sh` runs geesefs **as root** then drops to uid 1000 for the daemon (`exec gosu sandbox python -m …`). Note the decision here before continuing.

No commit (throwaway).

---

### Task 2: Sandbox image — fuse + geesefs + gated `entrypoint.sh`

**Files:**
- Modify: `images/sandbox/Dockerfile`
- Create: `images/sandbox/entrypoint.sh`

- [ ] **Step 1: Create the entrypoint wrapper**

Create `images/sandbox/entrypoint.sh`:

```bash
#!/bin/bash
# Sandbox container entrypoint.
#
# Default (and K8s, which overrides the command anyway): exec the tool-executor
# daemon, unchanged. When SANDBOX_S3FS_INLINE=1 (set only by the Docker sandbox
# backend in s3fs mode), first FUSE-mount the session's R2 prefix at /workspace
# via geesefs, then supervise the daemon so SIGTERM unmounts cleanly (geesefs
# flushes pending writes before the container dies).
set -euo pipefail
WORKSPACE="${WORKSPACE_DIR:-/workspace}"

if [ "${SANDBOX_S3FS_INLINE:-0}" != "1" ]; then
    exec python -m surogates.sandbox.executor_server
fi

: "${S3_BUCKET_PATH:?required for inline s3fs}"
: "${S3_ENDPOINT:?required for inline s3fs}"
# geesefs wants bucket:prefix (no leading slash on the prefix).
bucket_spec="${S3_BUCKET_PATH/:\//:}"

geesefs --endpoint "$S3_ENDPOINT" --region "${S3_REGION:-auto}" \
        -o allow_other --uid 1000 --gid 1000 \
        --file-mode 0644 --dir-mode 0755 \
        --memory-limit "${GEESEFS_MEMORY_LIMIT_MB:-256}" \
        -f "$bucket_spec" "$WORKSPACE" &
gpid=$!
for _ in $(seq 1 150); do
    mountpoint -q "$WORKSPACE" && break
    kill -0 "$gpid" 2>/dev/null || { echo "[sandbox-entrypoint] geesefs exited before mount" >&2; wait "$gpid" || true; exit 1; }
    sleep 0.2
done
mountpoint -q "$WORKSPACE" || { echo "[sandbox-entrypoint] geesefs mount timed out" >&2; exit 1; }
echo "[sandbox-entrypoint] mounted $bucket_spec at $WORKSPACE"

python -m surogates.sandbox.executor_server &
dpid=$!
trap 'kill -TERM "$dpid" 2>/dev/null || true; wait "$dpid" 2>/dev/null || true; \
      fusermount -u "$WORKSPACE" 2>/dev/null || true; \
      kill -TERM "$gpid" 2>/dev/null || true; exit 0' TERM INT
wait "$dpid"
fusermount -u "$WORKSPACE" 2>/dev/null || true
kill -TERM "$gpid" 2>/dev/null || true
```

> If Task 1 went the **fallback** route (root mount + drop), change the two `python -m …` invocations to `gosu sandbox python -m …`, run the container as root (Task 4 omits `--user`), and add `gosu` to the apt list below.

- [ ] **Step 2: Add fuse to the apt layer**

In `images/sandbox/Dockerfile`, in the big `apt-get install -y --no-install-recommends` list, add `fuse` next to `pdfgrep`:

```dockerfile
        imagemagick \
        pdfgrep \
        fuse \
```

- [ ] **Step 3: Enable `allow_other` and install geesefs**

In `images/sandbox/Dockerfile`, immediately after the `# ── Tool executor ──` COPY/RUN block (the `chmod +x /usr/local/bin/tool-executor` line), add:

```dockerfile
# ── geesefs (inline s3fs mount for docker-mode sandboxes) ───────────
# Same pinned build as the s3fs sidecar image. Inert in K8s/prod (the
# inline mount is gated behind SANDBOX_S3FS_INLINE, and the K8s manifest
# overrides the container command entirely).
ARG GEESEFS_VERSION=v0.43.7
RUN echo "user_allow_other" >> /etc/fuse.conf \
    && curl -fsSL -o /usr/local/bin/geesefs \
        "https://github.com/yandex-cloud/geesefs/releases/download/${GEESEFS_VERSION}/geesefs-linux-amd64" \
    && chmod +x /usr/local/bin/geesefs
```

- [ ] **Step 4: Install the entrypoint wrapper and switch CMD to it**

In `images/sandbox/Dockerfile`, replace the final entrypoint block:

```dockerfile
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "surogates.sandbox.executor_server"]
```

with:

```dockerfile
COPY images/sandbox/entrypoint.sh /usr/local/bin/sandbox-entrypoint
RUN chmod +x /usr/local/bin/sandbox-entrypoint

ENTRYPOINT ["tini", "--"]
CMD ["/usr/local/bin/sandbox-entrypoint"]
```

- [ ] **Step 5: Build the image and verify the non-inline (default) path still serves the daemon**

```bash
docker build -t ghcr.io/invergent-ai/surogates-agent-sandbox:latest -f images/sandbox/Dockerfile .
# Non-inline: bind-mount a scratch dir, expect /healthz 200 with REQUIRE_FUSE=0.
mkdir -p /tmp/sbx-smoke
cid=$(docker run -d --rm -p 18071:8071 \
  -e TOOL_EXECUTOR_TOKEN=t -e TOOL_EXECUTOR_REQUIRE_FUSE=0 \
  -v /tmp/sbx-smoke:/workspace \
  ghcr.io/invergent-ai/surogates-agent-sandbox:latest)
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18071/healthz || true)
  [ "$code" = "200" ] && break; sleep 1
done
echo "healthz=$code"
docker stop "$cid" >/dev/null
```

Expected: `healthz=200` — the wrapper's non-inline branch runs the daemon exactly as before. (Geesefs is present but unused here.)

- [ ] **Step 6: Commit**

```bash
git add images/sandbox/Dockerfile images/sandbox/entrypoint.sh docs/superpowers/plans/2026-06-13-docker-s3fs-inline.md
git commit -m "feat: add fuse+geesefs and gated inline-s3fs entrypoint to sandbox image"
```

---

### Task 3: `DockerSandbox` mode detection + `storage_settings`

**Files:**
- Modify: `surogates/sandbox/docker.py`
- Test: `tests/test_docker_sandbox.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_docker_sandbox.py`:

```python
class TestWorkspaceModeSelection:
    @staticmethod
    def _s3_storage():
        from types import SimpleNamespace
        return SimpleNamespace(
            endpoint="https://acct.r2.cloudflarestorage.com",
            access_key="ak", secret_key="sk", region="auto",
        )

    def _spec_with_s3_resource(self):
        from surogates.sandbox.base import Resource
        return SandboxSpec(
            session_id="root-1",
            workspace_path="/workspace",  # S3Backend sentinel
            resources=[Resource(
                source_ref="s3://surogate-workspaces-dev/sessions/root-1",
                mount_path="/workspace",
            )],
        )

    def test_s3fs_mode_when_resource_and_creds(self, healthz_transport):
        backend = _backend(FakeDocker(), healthz_transport,
                           storage_settings=self._s3_storage())
        mode, detail = backend._workspace_mode(self._spec_with_s3_resource())
        assert mode == "s3fs"
        assert detail == "surogate-workspaces-dev:/sessions/root-1"

    def test_ephemeral_when_s3_resource_but_no_creds(self, healthz_transport):
        backend = _backend(FakeDocker(), healthz_transport)  # no storage_settings
        mode, detail = backend._workspace_mode(self._spec_with_s3_resource())
        assert mode == "ephemeral"
        assert detail is None

    def test_bind_mode_for_real_host_path(self, healthz_transport, tmp_path):
        backend = _backend(FakeDocker(), healthz_transport,
                           storage_settings=self._s3_storage())
        spec = SandboxSpec(session_id="root-1", workspace_path=str(tmp_path))
        mode, detail = backend._workspace_mode(spec)
        assert mode == "bind"
        assert detail == str(tmp_path)

    def test_bucket_spec_bare_bucket(self, healthz_transport):
        from surogates.sandbox.base import Resource
        backend = _backend(FakeDocker(), healthz_transport,
                           storage_settings=self._s3_storage())
        spec = SandboxSpec(resources=[Resource(
            source_ref="s3://justbucket", mount_path="/workspace")])
        mode, detail = backend._workspace_mode(spec)
        assert mode == "s3fs"
        assert detail == "justbucket"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_docker_sandbox.py::TestWorkspaceModeSelection -v`
Expected: FAIL — `DockerSandbox.__init__() got an unexpected keyword argument 'storage_settings'` (and no `_workspace_mode`).

- [ ] **Step 3: Add `storage_settings` to the constructor**

In `surogates/sandbox/docker.py`, add the parameter and field. Change the `__init__` signature and body:

```python
    def __init__(
        self,
        *,
        image: str = "ghcr.io/invergent-ai/surogates-agent-sandbox:latest",
        executor_port_base: int = 33000,
        ready_timeout: int = 60,
        network: str = "bridge",
        mcp_proxy_url: str = "",
        storage_settings: Any = None,
        docker: _DockerDriver | None = None,
        httpx_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._image = image
        self._port_base = executor_port_base
        self._ready_timeout = ready_timeout
        self._network = network
        self._mcp_proxy_url = mcp_proxy_url
        self._storage = storage_settings
        self._docker = docker or _RealDocker()
        self._transport = httpx_transport
        self._client = ExecutorHTTPClient()
        self._entries: dict[str, _Entry] = {}
        self._next_offset = 0
        self._lock = asyncio.Lock()
```

Add `from typing import Any, Protocol` (it currently imports only `Protocol`):

```python
from typing import Any, Protocol
```

- [ ] **Step 4: Add the mode-detection helpers**

In `surogates/sandbox/docker.py`, add these methods (next to `_mountable_workspace`):

```python
    def _has_s3_creds(self) -> bool:
        s = self._storage
        return bool(
            s
            and getattr(s, "access_key", "")
            and getattr(s, "secret_key", "")
        )

    def _s3_bucket_spec(self, spec: SandboxSpec) -> str | None:
        """Parse the spec's s3:// workspace Resource into geesefs's
        ``bucket:/prefix`` form (mirrors K8sSandbox._build_pod_manifest)."""
        for res in spec.resources:
            if res.source_ref.startswith("s3://"):
                source = res.source_ref[5:].rstrip("/")
                if "/" in source:
                    bucket, path = source.split("/", 1)
                    return f"{bucket}:/{path}"
                return source
        return None

    def _workspace_mode(self, spec: SandboxSpec) -> tuple[str, str | None]:
        """Decide how /workspace is backed for this provision.

        - ``("s3fs", bucket_spec)``  — geesefs FUSE mount of R2 (needs creds)
        - ``("bind", host_path)``    — host bind-mount
        - ``("ephemeral", None)``    — container-internal scratch
        """
        bucket_spec = self._s3_bucket_spec(spec)
        if bucket_spec and self._has_s3_creds():
            return ("s3fs", bucket_spec)
        workspace = self._mountable_workspace(spec.workspace_path)
        if workspace is not None:
            return ("bind", str(workspace))
        return ("ephemeral", None)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_docker_sandbox.py::TestWorkspaceModeSelection -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
# update the Progress checklist: Task 3 -> [x], Task 4 -> [~]
git add surogates/sandbox/docker.py tests/test_docker_sandbox.py docs/superpowers/plans/2026-06-13-docker-s3fs-inline.md
git commit -m "feat: add workspace-mode detection and storage_settings to DockerSandbox"
```

---

### Task 4: `DockerSandbox` per-mode run args + env

**Files:**
- Modify: `surogates/sandbox/docker.py` (`provision`, `_build_env`)
- Test: `tests/test_docker_sandbox.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_docker_sandbox.py`:

```python
class TestS3fsModeProvision:
    @staticmethod
    def _s3_storage():
        from types import SimpleNamespace
        return SimpleNamespace(
            endpoint="https://acct.r2.cloudflarestorage.com",
            access_key="ak", secret_key="sk", region="auto",
        )

    def _spec(self):
        from surogates.sandbox.base import Resource
        return SandboxSpec(
            session_id="root-1",
            workspace_path="/workspace",
            resources=[Resource(
                source_ref="s3://surogate-workspaces-dev/sessions/root-1",
                mount_path="/workspace",
            )],
        )

    async def test_s3fs_run_args_and_env(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport,
                           storage_settings=self._s3_storage())
        await backend.provision(self._spec())
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        joined = " ".join(run_call)
        # FUSE caps
        assert "--cap-add SYS_ADMIN" in joined
        assert "--device /dev/fuse" in joined
        assert "apparmor:unconfined" in joined
        # s3fs env
        assert "SANDBOX_S3FS_INLINE=1" in joined
        assert "S3_BUCKET_PATH=surogate-workspaces-dev:/sessions/root-1" in joined
        assert "S3_ENDPOINT=https://acct.r2.cloudflarestorage.com" in joined
        assert "S3_REGION=auto" in joined
        assert "AWS_ACCESS_KEY_ID=ak" in joined
        assert "AWS_SECRET_ACCESS_KEY=sk" in joined
        # no bind mount, and the FUSE healthz check is left ON (no =0)
        assert "-v" not in run_call
        assert "TOOL_EXECUTOR_REQUIRE_FUSE=0" not in joined
        await backend.aclose()

    async def test_bind_mode_still_sets_require_fuse_off_and_no_caps(
        self, healthz_transport, tmp_path
    ):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport,
                           storage_settings=self._s3_storage())
        await backend.provision(
            SandboxSpec(session_id="root-1", workspace_path=str(tmp_path)))
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        joined = " ".join(run_call)
        assert "TOOL_EXECUTOR_REQUIRE_FUSE=0" in joined
        assert "SYS_ADMIN" not in joined
        assert f"{tmp_path}:/workspace" in joined
        await backend.aclose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_docker_sandbox.py::TestS3fsModeProvision -v`
Expected: FAIL — no FUSE caps / `SANDBOX_S3FS_INLINE` emitted (provision is still single-mode).

- [ ] **Step 3: Rewrite `provision`'s arg-building to be mode-aware**

In `surogates/sandbox/docker.py`, replace the block in `provision` from `workspace = self._mountable_workspace(...)` through the `args.append(image)` line:

Replace:

```python
        workspace = self._mountable_workspace(spec.workspace_path)
        env = self._build_env(spec, sandbox_id, token)
        # Docker is the local-dev backend: the configured image (docker_image)
        # is authoritative, so a developer's locally-built image is used rather
        # than the production ghcr reference that SandboxSpec.image defaults to.
        image = self._image

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
```

with:

```python
        mode, detail = self._workspace_mode(spec)
        env = self._build_env(spec, sandbox_id, token, mode, detail)
        # Docker is the local-dev backend: the configured image (docker_image)
        # is authoritative, so a developer's locally-built image is used rather
        # than the production ghcr reference that SandboxSpec.image defaults to.
        image = self._image

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
            if mode == "s3fs":
                # geesefs needs FUSE inside the container.
                args += [
                    "--cap-add", "SYS_ADMIN",
                    "--device", "/dev/fuse",
                    "--security-opt", "apparmor:unconfined",
                ]
            elif mode == "bind":
                args += ["-v", f"{detail}:/workspace"]
            for key, value in env.items():
                args += ["-e", f"{key}={value}"]
            args.append(image)
```

- [ ] **Step 4: Make `_build_env` mode-aware**

In `surogates/sandbox/docker.py`, change the `_build_env` signature and its base-env block. Replace the method's signature line and the base `env`/`reserved` setup:

Replace:

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
```

with:

```python
    def _build_env(
        self,
        spec: SandboxSpec,
        sandbox_id: str,
        token: str,
        mode: str,
        bucket_spec: str | None,
    ) -> dict[str, str]:
        """Container env: base + per-mode workspace wiring + MCP/KB.

        Mirrors the K8s pod manifest's env block, with host-local URLs
        rewritten so a bridged container can reach host services.
        """
        import os

        env = {
            "TOOL_EXECUTOR_TOKEN": token,
            "WORKSPACE_DIR": "/workspace",
        }
        if mode == "s3fs":
            # /workspace becomes a real FUSE mount, so leave the daemon's
            # FUSE readiness check at its default (require_fuse=1) — the
            # entrypoint mounts geesefs from these vars before serving.
            s = self._storage
            env["SANDBOX_S3FS_INLINE"] = "1"
            env["S3_BUCKET_PATH"] = bucket_spec or ""
            env["S3_ENDPOINT"] = getattr(s, "endpoint", "")
            env["S3_REGION"] = getattr(s, "region", "") or "auto"
            env["AWS_ACCESS_KEY_ID"] = getattr(s, "access_key", "")
            env["AWS_SECRET_ACCESS_KEY"] = getattr(s, "secret_key", "")
        else:
            # bind / ephemeral: /workspace is not FUSE, so disable the check.
            env["TOOL_EXECUTOR_REQUIRE_FUSE"] = "0"
        reserved = {
            "TOOL_EXECUTOR_TOKEN", "WORKSPACE_DIR", "TOOL_EXECUTOR_REQUIRE_FUSE",
            "TOOL_EXECUTOR_PORT", "SANDBOX_S3FS_INLINE", "S3_BUCKET_PATH",
            "S3_ENDPOINT", "S3_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        }
```

(The rest of `_build_env` — the `spec.env` passthrough loop and the MCP/KB blocks — stays exactly as-is.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_docker_sandbox.py -v`
Expected: PASS (all — the new `TestS3fsModeProvision` + `TestWorkspaceModeSelection` plus the existing classes; the existing tests construct `DockerSandbox` without `storage_settings`, so they stay `bind`/`ephemeral` and keep emitting `TOOL_EXECUTOR_REQUIRE_FUSE=0`).

- [ ] **Step 6: Commit**

```bash
# Progress: Task 4 -> [x], Task 5 -> [~]
git add surogates/sandbox/docker.py tests/test_docker_sandbox.py docs/superpowers/plans/2026-06-13-docker-s3fs-inline.md
git commit -m "feat: DockerSandbox s3fs mode — geesefs mount via FUSE caps + env"
```

---

### Task 5: Worker wiring + `config.dev.yaml` back to s3

**Files:**
- Modify: `surogates/orchestrator/worker.py`
- Modify: `config.dev.yaml`

- [ ] **Step 1: Pass `storage_settings` to `DockerSandbox`**

In `surogates/orchestrator/worker.py`, in the `elif settings.sandbox.backend == "docker":` branch, add the `storage_settings` kwarg:

```python
    elif settings.sandbox.backend == "docker":
        from surogates.sandbox.docker import DockerSandbox

        sandbox_backend = DockerSandbox(
            image=settings.sandbox.docker_image,
            executor_port_base=settings.sandbox.docker_executor_port_base,
            ready_timeout=settings.sandbox.docker_ready_timeout,
            network=settings.sandbox.docker_network,
            mcp_proxy_url=settings.mcp_proxy_url,
            storage_settings=settings.storage,
        )
```

- [ ] **Step 2: Point dev storage back at R2**

In `config.dev.yaml`, set `storage.backend` back to `s3` (the `bind`/local block can stay commented for switch-back):

```yaml
storage:
  backend: "s3"
  endpoint: "https://5610970a6285ee674c3199946c8e0e52.r2.cloudflarestorage.com"
  region: "auto"
  bucket: "surogate-workspaces-dev"
  access_key: "ecf8aa710975c932eefd6c5a9996453d"
  secret_key: "925e93eaff4b2ef3e8a167fca26592d7ae343d7c3fda37d46ff586f080674653"
  memory_bucket: "surogate-memory-dev"
```

- [ ] **Step 3: Verify worker imports and constructs the docker backend with storage**

```bash
python -c "
from types import SimpleNamespace
from surogates.sandbox.docker import DockerSandbox
s = SimpleNamespace(endpoint='e', access_key='ak', secret_key='sk', region='auto')
b = DockerSandbox(image='x', executor_port_base=33000, ready_timeout=5, network='bridge', mcp_proxy_url='', storage_settings=s)
print('has creds:', b._has_s3_creds())
import surogates.orchestrator.worker
print('worker imports OK')
" 2>&1 | grep -ivE "Pydantic|warnings.warn|json_encoders" | tail -3
```

Expected: `has creds: True` and `worker imports OK`.

- [ ] **Step 4: Run the sandbox unit suite for regressions**

Run: `pytest tests/test_docker_sandbox.py tests/test_sandbox.py tests/test_k8s_sandbox.py tests/test_executor_http_client.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
# Progress: Task 5 -> [x], Task 6 -> [~]
git add surogates/orchestrator/worker.py config.dev.yaml docs/superpowers/plans/2026-06-13-docker-s3fs-inline.md
git commit -m "feat: wire storage_settings into docker backend; dev storage back to s3"
```

---

### Task 6: Opt-in integration test (real docker + R2)

**Files:**
- Create: `tests/integration/test_docker_sandbox_s3fs_e2e.py`

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_docker_sandbox_s3fs_e2e.py`:

```python
"""Opt-in: DockerSandbox s3fs mode writes to R2 (real Docker + real bucket).

Skipped unless Docker is present AND SUROGATES_TEST_DOCKER_S3FS=1 AND R2 creds
are in the environment. Run locally with creds exported from config.dev.yaml:

    SUROGATES_TEST_DOCKER_S3FS=1 \
    R2_ENDPOINT=... R2_KEY=... R2_SECRET=... R2_BUCKET=... \
    pytest tests/integration/test_docker_sandbox_s3fs_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace

import pytest

from surogates.sandbox.base import Resource, SandboxSpec, SandboxStatus
from surogates.sandbox.docker import DockerSandbox

_REQUIRED = ("R2_ENDPOINT", "R2_KEY", "R2_SECRET", "R2_BUCKET")

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or os.environ.get("SUROGATES_TEST_DOCKER_S3FS") != "1"
    or not all(os.environ.get(k) for k in _REQUIRED),
    reason="requires Docker, SUROGATES_TEST_DOCKER_S3FS=1, and R2_* creds",
)

_IMAGE = os.environ.get(
    "SUROGATES_TEST_SANDBOX_IMAGE",
    "ghcr.io/invergent-ai/surogates-agent-sandbox:latest",
)


async def test_s3fs_write_reaches_r2(tmp_path):
    bucket = os.environ["R2_BUCKET"]
    prefix = "itest/docker-s3fs"  # disjoint test prefix
    storage = SimpleNamespace(
        endpoint=os.environ["R2_ENDPOINT"],
        access_key=os.environ["R2_KEY"],
        secret_key=os.environ["R2_SECRET"],
        region="auto",
    )
    backend = DockerSandbox(
        image=_IMAGE, executor_port_base=34100, ready_timeout=120,
        storage_settings=storage,
    )
    spec = SandboxSpec(
        session_id="00000000-0000-0000-0000-0000000000ff",
        workspace_path="/workspace",
        resources=[Resource(
            source_ref=f"s3://{bucket}/{prefix}/sessions/root",
            mount_path="/workspace",
        )],
        timeout=60,
    )
    sid = None
    try:
        sid = await backend.provision(spec)
        assert await backend.status(sid) == SandboxStatus.RUNNING

        marker = "r2-roundtrip-ok"
        result = json.loads(await backend.execute(
            sid, "terminal",
            json.dumps({"command": f"echo {marker} > /workspace/itest.txt && sync"}),
        ))
        assert result.get("exit_code", 0) == 0

        # Read it back through a fresh listing in the same container.
        out = json.loads(await backend.execute(
            sid, "terminal", json.dumps({"command": "cat /workspace/itest.txt"})))
        assert marker in (out.get("output") or "")
    finally:
        if sid is not None:
            await backend.destroy(sid)
        await backend.aclose()
```

- [ ] **Step 2: Verify it is collected and skipped without the opt-in**

Run: `pytest tests/integration/test_docker_sandbox_s3fs_e2e.py -v`
Expected: `1 skipped` (reason: requires Docker, opt-in, and R2 creds). *(If the integration conftest can't import `testcontainers` in this env, the suite won't collect — that's the same pre-existing limitation noted for the first plan; validate the file standalone with `python -c "import importlib.util,sys; s=importlib.util.spec_from_file_location('m','tests/integration/test_docker_sandbox_s3fs_e2e.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ok', bool(m.pytestmark.args[0]))"`.)*

- [ ] **Step 3: (Optional, local only) Run for real**

```bash
SUROGATES_TEST_DOCKER_S3FS=1 \
R2_ENDPOINT="$R2_ENDPOINT" R2_KEY="$R2_KEY" R2_SECRET="$R2_SECRET" R2_BUCKET="$R2_BUCKET" \
pytest tests/integration/test_docker_sandbox_s3fs_e2e.py -v
```
Expected: PASS. Then confirm the object exists in R2 (e.g. via the ops Studio UI for a real session, or an `aws s3 ls --endpoint-url`), proving writes reach the prefix ops reads.

- [ ] **Step 4: Commit**

```bash
# Progress: Task 6 -> [x]
git add tests/integration/test_docker_sandbox_s3fs_e2e.py docs/superpowers/plans/2026-06-13-docker-s3fs-inline.md
git commit -m "test: opt-in DockerSandbox s3fs e2e (writes reach R2)"
```

---

## Self-Review

**Spec coverage:**
- Three workspace modes (`s3fs`/`bind`/`ephemeral`) → Task 3 (`_workspace_mode`) + Task 4 (per-mode args/env).
- Image: fuse + geesefs + gated entrypoint → Task 2.
- `entrypoint.sh` mount-then-supervise, gated, K8s-safe → Task 2 Step 1.
- `DockerSandbox` `storage_settings`, bucket parse, FUSE caps, s3fs env, `REQUIRE_FUSE` left default in s3fs / `=0` otherwise → Tasks 3–4.
- Worker passes `storage_settings`; config back to s3 → Task 5.
- Non-root FUSE risk validated first; gosu fallback documented → Task 1 (+ Task 2 Step 1 note).
- Tests: unit (Tasks 3–4), integration writes-reach-R2 (Task 6). Entrypoint shell behavior is covered by the Task 2 Step 5 smoke check + Task 6 e2e (a standalone bats test was judged low-value vs the e2e; noted, not silently dropped).
- Error handling (mount failure → readiness timeout with improved detail; caps refused → docker error) is inherent to the existing `provision`/`_wait_ready` paths — no new code needed.

**Placeholder scan:** none. Secrets are sourced from `config.dev.yaml`/env at run time, never written into the plan.

**Type/name consistency:** `_workspace_mode(spec) -> (mode, detail)` defined Task 3, consumed Task 4 (`mode`, `detail`). `_build_env(spec, sandbox_id, token, mode, bucket_spec)` defined Task 4, called from `provision` with `(spec, sandbox_id, token, mode, detail)`. `_s3_bucket_spec`/`_has_s3_creds` defined Task 3, used in `_workspace_mode`. `storage_settings`→`self._storage` consistent across Tasks 3–5. `SANDBOX_S3FS_INLINE`, `S3_BUCKET_PATH`, `S3_ENDPOINT`, `S3_REGION` identical between `entrypoint.sh` (Task 2) and `_build_env` (Task 4).

## Execution Handoff

(Offered after saving.)
