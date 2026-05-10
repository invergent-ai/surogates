# Agent Browser — Phase B: Kubernetes Backend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Implementation TODO

- [x] **Plan review fixes** — corrected stale Phase B plan issues before implementation.
- [ ] **Task 1: K8sBrowserBackend skeleton** — pending.
- [ ] **Task 2: Pod manifest builder** — pending.
- [ ] **Task 3: Service manifest builder** — pending.
- [ ] **Task 4: K8s provision lifecycle** — pending.
- [ ] **Task 5: BrowserBackend protocol alignment** — pending.
- [ ] **Task 6: K8s status mapping** — pending.
- [ ] **Task 7: K8s destroy lifecycle** — pending.
- [ ] **Task 8: find_by_session fallback primitive** — pending.
- [ ] **Task 9: Worker bootstrap wiring** — pending.
- [ ] **Task 10: Browser image packaging** — pending.
- [ ] **Task 11: Helm browser ServiceAccount** — pending.
- [ ] **Task 12: Helm browser NetworkPolicy** — pending.
- [ ] **Task 13: Helm worker RBAC services** — pending.
- [ ] **Task 14: Helm values and worker env** — pending.
- [ ] **Task 15: Opt-in K8s e2e** — pending.
- [ ] **Final verification** — pending.

**Goal:** Land the production deployment of the agent browser. Add a `K8sBrowserBackend` that provisions per-session pods + per-session Services in the cluster, wire it into the worker behind `browser.backend = "kubernetes"`, ship a custom `surogates-agent-browser` container image alongside the existing api/worker/sandbox/s3fs images, ship the helm chart pieces (browser ServiceAccount, NetworkPolicy, worker RBAC extensions, values defaults) for both the in-repo chart and the surogate-ops chart, and add a label-keyed `find_by_session` lookup that the Phase C API server uses as a stale-Redis fallback. End state: the same agent that drove a docker-launched browser in Phase A drives a per-session pod in K8s, with no behavioural change visible to the LLM.

**Architecture:** `K8sBrowserBackend` mirrors `surogates/sandbox/kubernetes.py` for shape. One pod and one ClusterIP Service per session. The Service exposes three ports — `10001` (REST, used now by the worker), `9222` (CDP, reserved), `443 → 6080` (NoVNC live view, used by Phase C). Pods are labelled `app=surogates-browser`, `surogates.ai/session-id`, `/org-id`, `/user-id`; the Helm NetworkPolicy must select that literal `app=surogates-browser` label because these pods are created by Python, not a Helm Deployment. NetworkPolicy: ingress to browser pods from worker + api-server only; egress to internet so Chromium can browse. Worker RBAC: Roles for `pods` and `services` (create/get/list/watch/delete) on top of the existing sandbox RBAC.

**Tech Stack:** kubernetes-asyncio (already a dep — see `surogates/sandbox/kubernetes.py`), pytest (mocked K8s API for unit tests, opt-in real-cluster integration test), helm (templates duplicated across the in-repo and surogate-ops charts).

**Spec:** [`docs/superpowers/specs/2026-05-10-agent-browser-design.md`](../specs/2026-05-10-agent-browser-design.md)

**Predecessor:** [Phase A](2026-05-10-agent-browser-phase-a.md) — must be merged before this plan starts. Phase A ships the `BrowserBackend` protocol, `BrowserPool`, `BrowserRegistry`, `BrowserControlStore`, `KernelBrowserClient`, and the discrete tools. Phase B only adds the K8s backend implementation; nothing else changes.

---

## File Structure

```
surogates/browser/
└── kubernetes.py                 (NEW — K8sBrowserBackend)

surogates/browser/registry.py     (no change — already supports the K8s fallback path
                                   because find_by_session lives on the backend)
surogates/browser/base.py         (MODIFY — align BrowserBackend.provision protocol with
                                   K8s labels and update BrowserSpec image default)

surogates/orchestrator/worker.py  (MODIFY — instantiate K8sBrowserBackend when
                                   settings.browser.backend == "kubernetes")

surogates/config.py               (MODIFY — bump BrowserSettings.image default to
                                   ghcr.io/invergent-ai/surogates-agent-browser:latest)

images/
└── browser/
    ├── Dockerfile                (NEW — FROM kernel-images upstream + zstd + branding)
images/build.sh                   (MODIFY — add "browser" → "surogates-agent-browser"
                                   to the IMAGES map)

helm/surogates/templates/
├── browser-rbac.yaml             (NEW — ServiceAccount for the browser pod)
├── browser-networkpolicy.yaml    (NEW — ingress: worker + api; egress: internet + DNS)
├── worker-rbac.yaml              (MODIFY — add services verbs for browser Services)
├── worker-deployment.yaml        (MODIFY — pass SUROGATES_BROWSER_* env vars to worker)
└── values.yaml                   (MODIFY — add browser.* knobs)

# DUPLICATE every helm change into the surogate-ops chart:
/work/surogate-ops/surogate_ops/agent_chart/templates/
├── browser-rbac.yaml             (NEW — same content as in-repo chart)
├── browser-networkpolicy.yaml    (NEW)
├── worker-rbac.yaml              (MODIFY)
├── worker-deployment.yaml        (MODIFY)
└── values.yaml                   (MODIFY)

tests/test_browser_kubernetes.py  (NEW — pod + service manifest builders, status mapping,
                                   provision happy-path with mocked K8s API)
tests/integration/test_browser_e2e_k8s.py
                                  (NEW — opt-in marker, requires kind/minikube)
```

The two helm charts (`helm/surogates/` in this repo and
`surogate_ops/agent_chart/` in the surogate-ops repo) currently share
`sandbox-rbac.yaml`, `sandbox-networkpolicy.yaml`, and `worker-rbac.yaml`
verbatim. That parity is load-bearing — the surogate-ops chart drives
production deploys, the in-repo chart drives local dev/CI. Phase B keeps
the parity: every browser-related helm change is applied to both
locations and committed in two separate `git commit` calls (one per
repo). The plan calls this out at every helm task.

---

## Conventions used in every task

- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`) — same as Phase A.
- K8s mocks: pattern from `tests/test_k8s_sandbox.py` (`unittest.mock.MagicMock` and `AsyncMock` over the kubernetes-asyncio API client).
- Manifest tests assert on the constructed `client.V1Pod` / `client.V1Service` object — not on the JSON serialisation. The `kubernetes_asyncio.client` types provide structured field access.
- Commit at the end of every task with the message shown. For the helm tasks (11–14), commit twice — once in `/work/surogates`, once in `/work/surogate-ops`. Each task spells out both commits.
- Use `uv run pytest ...` in this repo; `pytest` is not guaranteed to be on `PATH`.
- Run `uv run pytest tests/test_browser_kubernetes.py -v` after every backend task. The full suite command is documented at the end, but this repo currently has unrelated integration failures when `storage.bucket` is unset, so trust focused browser + harness tests for this phase unless the environment has the full integration config.

---

## Task 1: `K8sBrowserBackend` skeleton — class, K8s client lazy-loader

**Files:**
- Create: `surogates/browser/kubernetes.py`
- Test: `tests/test_browser_kubernetes.py`

- [ ] **Step 1: Write the failing test**

`tests/test_browser_kubernetes.py`:

```python
"""Tests for surogates.browser.kubernetes.K8sBrowserBackend.

Uses mocks for kubernetes-asyncio so the suite runs without a cluster.
The real-cluster integration test lives at
``tests/integration/test_browser_e2e_k8s.py`` behind the ``browser_e2e_k8s``
marker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)
from surogates.browser.kubernetes import K8sBrowserBackend


@pytest.fixture()
def backend() -> K8sBrowserBackend:
    return K8sBrowserBackend(
        namespace="test-ns",
        service_account="test-browser-sa",
        pod_ready_timeout=5,
        image="kernel-headful:test",
    )


class TestSkeleton:
    def test_construct(self, backend: K8sBrowserBackend) -> None:
        assert backend._namespace == "test-ns"
        assert backend._service_account == "test-browser-sa"
        assert backend._pod_ready_timeout == 5
        assert backend._image == "kernel-headful:test"
        assert backend._pods == {}

    async def test_get_api_caches(self, backend: K8sBrowserBackend, monkeypatch) -> None:
        from kubernetes_asyncio import client as k8s_client, config as k8s_config

        # Pretend in-cluster config works.
        monkeypatch.setattr(k8s_config, "load_incluster_config", lambda: None)
        api = await backend._get_api()
        api2 = await backend._get_api()
        assert api is api2  # cached
        assert isinstance(api, k8s_client.CoreV1Api)
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_browser_kubernetes.py -v` → 2 FAIL with `ImportError`.

- [ ] **Step 3: Implement the skeleton**

`surogates/browser/kubernetes.py`:

```python
"""Kubernetes backend for the agent browser.

One pod and one ClusterIP Service per session. The Service exposes
three ports — ``10001`` (REST), ``9222`` (CDP), ``443 → 6080`` (NoVNC
live view) — all internal to the cluster. Worker reaches the pod via
the Service DNS (``browser-{id[:12]}.{namespace}.svc:10001``).

Mirrors :class:`surogates.sandbox.kubernetes.K8sSandbox` for shape so
operators can reason about both the same way.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from kubernetes_asyncio import client, config, watch
from kubernetes_asyncio.client import ApiException

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)

logger = logging.getLogger(__name__)


# Cluster-internal port numbers used inside the Service. The targetPorts
# point at the kernel-images container's actual listen ports.
SERVICE_PORT_REST = 10001
SERVICE_PORT_CDP = 9222
SERVICE_PORT_LIVE_VIEW = 443
TARGET_PORT_LIVE_VIEW_NOVNC = 6080


@dataclass
class _PodEntry:
    browser_id: str
    pod_name: str
    service_name: str
    namespace: str
    spec: BrowserSpec
    endpoint: BrowserEndpoint
    status: BrowserStatus = BrowserStatus.PENDING


class K8sBrowserBackend:
    def __init__(
        self,
        *,
        namespace: str = "surogates",
        service_account: str = "surogates-browser",
        pod_ready_timeout: int = 60,
        image: str = "ghcr.io/onkernel/chromium-headful:stable",
    ) -> None:
        self._namespace = namespace
        self._service_account = service_account
        self._pod_ready_timeout = pod_ready_timeout
        self._image = image
        self._pods: dict[str, _PodEntry] = {}
        self._api: client.CoreV1Api | None = None

    # ------------------------------------------------------------------
    # K8s client
    # ------------------------------------------------------------------

    async def _get_api(self) -> client.CoreV1Api:
        """Return a cached ``CoreV1Api`` client.

        Tries in-cluster config first (production), falls back to
        kubeconfig (local dev running outside the cluster). Raises
        ``BrowserUnavailableError`` if neither path succeeds so the
        harness reports an infra failure rather than leaking a raw
        ``ConfigException`` to the LLM.
        """
        if self._api is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                try:
                    await config.load_kube_config()
                except Exception as exc:
                    raise BrowserUnavailableError(
                        f"Kubernetes browser backend unavailable — could not "
                        f"load kubeconfig: {exc}.",
                    ) from exc
            self._api = client.CoreV1Api()
        return self._api
```

- [ ] **Step 4: Run** — both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/kubernetes.py tests/test_browser_kubernetes.py
git commit -m "feat(browser): add K8sBrowserBackend skeleton"
```

---

## Task 2: Pod manifest builder

**Files:**
- Modify: `surogates/browser/kubernetes.py`
- Test: `tests/test_browser_kubernetes.py` (extend)

- [ ] **Step 1: Write the failing test** — append:

```python
class TestBuildPodManifest:
    def test_basic_manifest(self, backend: K8sBrowserBackend) -> None:
        spec = BrowserSpec(
            image="kernel-headful:test",
            cpu="500m", memory="1Gi",
            cpu_limit="1", memory_limit="2Gi",
            active_deadline_seconds=1800,
            env={"FOO": "bar"},
        )
        pod = backend._build_pod_manifest(
            browser_id="abc123def456",
            pod_name="browser-abc123def456",
            session_id="sess-1",
            org_id="org-1",
            user_id="user-1",
            spec=spec,
        )
        assert pod.metadata.name == "browser-abc123def456"
        assert pod.metadata.namespace == "test-ns"
        # Required labels for label-based fallback in Phase C.
        assert pod.metadata.labels == {
            "app": "surogates-browser",
            "surogates.ai/browser-id": "abc123def456",
            "surogates.ai/session-id": "sess-1",
            "surogates.ai/org-id": "org-1",
            "surogates.ai/user-id": "user-1",
        }
        assert pod.spec.service_account_name == "test-browser-sa"
        assert pod.spec.restart_policy == "Never"
        assert pod.spec.active_deadline_seconds == 1800

        assert len(pod.spec.containers) == 1
        c = pod.spec.containers[0]
        assert c.name == "browser"
        assert c.image == "kernel-headful:test"
        assert c.image_pull_policy == "IfNotPresent"
        assert c.resources.requests == {"cpu": "500m", "memory": "1Gi"}
        assert c.resources.limits == {"cpu": "1", "memory": "2Gi"}
        # Three ports exposed for the Service to target.
        port_numbers = sorted(p.container_port for p in c.ports)
        assert port_numbers == [6080, 9222, 10001]
        # User-supplied env propagated.
        env = {e.name: e.value for e in c.env}
        assert env["FOO"] == "bar"

    def test_manifest_uses_backend_default_image_when_spec_blank(
        self, backend: K8sBrowserBackend
    ) -> None:
        # Force a "blank" spec image to trigger the fallback to backend default.
        spec = BrowserSpec(image="")
        pod = backend._build_pod_manifest(
            browser_id="abc", pod_name="browser-abc",
            session_id="s", org_id="o", user_id="u", spec=spec,
        )
        assert pod.spec.containers[0].image == "kernel-headful:test"

    def test_manifest_falls_back_to_default_active_deadline(
        self, backend: K8sBrowserBackend
    ) -> None:
        spec = BrowserSpec()  # default 3600
        pod = backend._build_pod_manifest(
            browser_id="x", pod_name="browser-x",
            session_id="s", org_id="o", user_id="u", spec=spec,
        )
        assert pod.spec.active_deadline_seconds == 3600
```

- [ ] **Step 2: Run** — 3 FAIL.

- [ ] **Step 3: Implement** — append to `surogates/browser/kubernetes.py`:

```python
    # ------------------------------------------------------------------
    # Manifest builders
    # ------------------------------------------------------------------

    def _build_pod_manifest(
        self,
        *,
        browser_id: str,
        pod_name: str,
        session_id: str,
        org_id: str,
        user_id: str,
        spec: BrowserSpec,
    ) -> client.V1Pod:
        """Build the pod manifest for a single browser session.

        Labels are essential for the Phase C API server's stale-Redis
        fallback path: when the registry is missing an entry, the API
        server lists pods by ``surogates.ai/session-id`` to recover
        the routing target.
        """
        image = spec.image or self._image

        env_vars = [
            client.V1EnvVar(name=k, value=v) for k, v in spec.env.items()
        ]

        container = client.V1Container(
            name="browser",
            image=image,
            image_pull_policy="IfNotPresent",
            ports=[
                client.V1ContainerPort(container_port=10001, name="rest"),
                client.V1ContainerPort(container_port=9222, name="cdp"),
                client.V1ContainerPort(container_port=6080, name="novnc"),
            ],
            resources=client.V1ResourceRequirements(
                requests={"cpu": spec.cpu, "memory": spec.memory},
                limits={"cpu": spec.cpu_limit, "memory": spec.memory_limit},
            ),
            env=env_vars,
            # Tell K8s the pod is ready when the kernel-images REST API
            # responds. The /spec.json path always returns 200 once the
            # Go server is up — same probe ProcessBrowserBackend uses.
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/spec.json", port=10001),
                period_seconds=2,
                failure_threshold=30,
            ),
        )

        return client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=self._namespace,
                labels={
                    "app": "surogates-browser",
                    "surogates.ai/browser-id": browser_id,
                    "surogates.ai/session-id": session_id,
                    "surogates.ai/org-id": org_id,
                    "surogates.ai/user-id": user_id,
                },
            ),
            spec=client.V1PodSpec(
                service_account_name=self._service_account,
                restart_policy="Never",
                active_deadline_seconds=spec.active_deadline_seconds,
                containers=[container],
            ),
        )
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/kubernetes.py tests/test_browser_kubernetes.py
git commit -m "feat(browser): build K8s pod manifest with discovery labels"
```

---

## Task 3: Service manifest builder

**Files:**
- Modify: `surogates/browser/kubernetes.py`
- Test: `tests/test_browser_kubernetes.py` (extend)

- [ ] **Step 1: Write the failing test** — append:

```python
class TestBuildServiceManifest:
    def test_basic_service(self, backend: K8sBrowserBackend) -> None:
        svc = backend._build_service_manifest(
            browser_id="abc123def456",
            service_name="browser-abc123def456",
            session_id="sess-1",
            org_id="org-1",
            user_id="user-1",
        )
        assert svc.metadata.name == "browser-abc123def456"
        assert svc.metadata.namespace == "test-ns"
        # Service inherits the same discovery labels as the pod.
        assert svc.metadata.labels["surogates.ai/session-id"] == "sess-1"
        assert svc.metadata.labels["surogates.ai/browser-id"] == "abc123def456"

        # Selector matches exactly the pod's labels for this browser.
        assert svc.spec.selector == {
            "app": "surogates-browser",
            "surogates.ai/browser-id": "abc123def456",
        }
        assert svc.spec.type == "ClusterIP"

        # Three ports exposed.
        ports = {p.name: (p.port, p.target_port) for p in svc.spec.ports}
        assert ports["rest"] == (10001, 10001)
        assert ports["cdp"] == (9222, 9222)
        # Live-view port: external 443, target 6080 (NoVNC v1 — see spec §4.1).
        assert ports["live-view"] == (443, 6080)
```

- [ ] **Step 2: Run** — 1 FAIL.

- [ ] **Step 3: Implement** — append:

```python
    def _build_service_manifest(
        self,
        *,
        browser_id: str,
        service_name: str,
        session_id: str,
        org_id: str,
        user_id: str,
    ) -> client.V1Service:
        """Build a ClusterIP Service that targets exactly this browser pod.

        The selector is keyed on ``surogates.ai/browser-id`` so a stale
        pod deletion + new pod creation (rare; can't happen in v1 because
        we destroy the service alongside the pod) doesn't accidentally
        re-route to a sibling session's pod.
        """
        return client.V1Service(
            metadata=client.V1ObjectMeta(
                name=service_name,
                namespace=self._namespace,
                labels={
                    "app": "surogates-browser",
                    "surogates.ai/browser-id": browser_id,
                    "surogates.ai/session-id": session_id,
                    "surogates.ai/org-id": org_id,
                    "surogates.ai/user-id": user_id,
                },
            ),
            spec=client.V1ServiceSpec(
                type="ClusterIP",
                selector={
                    "app": "surogates-browser",
                    "surogates.ai/browser-id": browser_id,
                },
                ports=[
                    client.V1ServicePort(
                        name="rest",
                        port=SERVICE_PORT_REST,
                        target_port=SERVICE_PORT_REST,
                        protocol="TCP",
                    ),
                    client.V1ServicePort(
                        name="cdp",
                        port=SERVICE_PORT_CDP,
                        target_port=SERVICE_PORT_CDP,
                        protocol="TCP",
                    ),
                    client.V1ServicePort(
                        name="live-view",
                        port=SERVICE_PORT_LIVE_VIEW,
                        target_port=TARGET_PORT_LIVE_VIEW_NOVNC,
                        protocol="TCP",
                    ),
                ],
            ),
        )
```

- [ ] **Step 4: Run** — PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/kubernetes.py tests/test_browser_kubernetes.py
git commit -m "feat(browser): build ClusterIP service mapping for live-view + REST + CDP"
```

---

## Task 4: `provision()` — create pod + service, wait for ready

**Files:**
- Modify: `surogates/browser/kubernetes.py`
- Test: `tests/test_browser_kubernetes.py` (extend)

- [ ] **Step 1: Write the failing test** — append:

```python
class TestProvision:
    async def test_provision_creates_pod_and_service(
        self, backend: K8sBrowserBackend, monkeypatch
    ) -> None:
        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()

        async def fake_get_api() -> "MagicMock":
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        spec = BrowserSpec(image="kernel-headful:test")
        bid, endpoint = await backend.provision(
            spec, session_id="sess-1", org_id="org-1", user_id="user-1",
        )
        # browser_id is 32-hex.
        assert len(bid) == 32
        # Endpoint URLs use the cluster-internal Service DNS.
        prefix = f"browser-{bid[:12]}.test-ns.svc"
        assert endpoint.rest_url == f"http://{prefix}:10001"
        assert endpoint.cdp_url == f"ws://{prefix}:9222"
        assert endpoint.live_view_url == f"ws://{prefix}:443"

        # Pod and Service were both created, in that order.
        assert api.create_namespaced_pod.call_count == 1
        assert api.create_namespaced_service.call_count == 1

    async def test_provision_rolls_back_pod_on_service_failure(
        self, backend: K8sBrowserBackend, monkeypatch
    ) -> None:
        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        # Service creation fails.
        api.create_namespaced_service = AsyncMock(
            side_effect=ApiException(status=500, reason="boom"),
        )
        api.delete_namespaced_pod = AsyncMock()

        async def fake_get_api() -> "MagicMock":
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        with pytest.raises(BrowserUnavailableError):
            await backend.provision(
                BrowserSpec(), session_id="s", org_id="o", user_id="u",
            )
        # The pod we created was rolled back.
        assert api.delete_namespaced_pod.call_count == 1

    async def test_provision_rolls_back_when_pod_never_ready(
        self, backend: K8sBrowserBackend, monkeypatch
    ) -> None:
        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()
        api.delete_namespaced_pod = AsyncMock()
        api.delete_namespaced_service = AsyncMock()

        async def fake_get_api() -> "MagicMock":
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            raise RuntimeError("did not become ready")

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        with pytest.raises(BrowserUnavailableError):
            await backend.provision(
                BrowserSpec(), session_id="s", org_id="o", user_id="u",
            )
        assert api.delete_namespaced_service.call_count == 1
        assert api.delete_namespaced_pod.call_count == 1
```

- [ ] **Step 2: Run** — 3 FAIL.

- [ ] **Step 3: Implement** — append:

```python
    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # Note on the protocol: ``BrowserBackend.provision`` from Phase A
    # takes only ``spec``, but K8s provisioning needs labels (session,
    # org, user). This direct K8s unit test is intentionally added before
    # the protocol refactor; Task 5 immediately updates the shared
    # protocol, BrowserPool, and ProcessBrowserBackend so runtime dispatch
    # has one consistent call shape.
    async def provision(
        self,
        spec: BrowserSpec,
        *,
        session_id: str,
        org_id: str,
        user_id: str,
    ) -> tuple[str, BrowserEndpoint]:
        api = await self._get_api()
        browser_id = uuid.uuid4().hex
        suffix = browser_id[:12]
        pod_name = f"browser-{suffix}"
        service_name = f"browser-{suffix}"  # same name; pod + svc co-located

        endpoint = BrowserEndpoint(
            rest_url=f"http://{service_name}.{self._namespace}.svc:{SERVICE_PORT_REST}",
            cdp_url=f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_CDP}",
            live_view_url=f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_LIVE_VIEW}",
        )

        pod_manifest = self._build_pod_manifest(
            browser_id=browser_id, pod_name=pod_name,
            session_id=session_id, org_id=org_id, user_id=user_id, spec=spec,
        )
        try:
            await api.create_namespaced_pod(self._namespace, pod_manifest)
        except ApiException as exc:
            raise BrowserUnavailableError(
                f"Failed to create browser pod {pod_name}: {exc}",
            ) from exc

        svc_manifest = self._build_service_manifest(
            browser_id=browser_id, service_name=service_name,
            session_id=session_id, org_id=org_id, user_id=user_id,
        )
        try:
            await api.create_namespaced_service(self._namespace, svc_manifest)
        except ApiException as exc:
            # Roll back the pod we just created.
            await self._delete_pod_safe(api, pod_name)
            raise BrowserUnavailableError(
                f"Failed to create browser service {service_name}: {exc}",
            ) from exc

        try:
            await self._wait_for_ready(api, pod_name)
        except Exception as exc:
            # Roll back both resources.
            await self._delete_service_safe(api, service_name)
            await self._delete_pod_safe(api, pod_name)
            raise BrowserUnavailableError(
                f"Browser pod {pod_name} did not become ready: {exc}",
            ) from exc

        entry = _PodEntry(
            browser_id=browser_id, pod_name=pod_name,
            service_name=service_name, namespace=self._namespace,
            spec=spec, endpoint=endpoint, status=BrowserStatus.RUNNING,
        )
        self._pods[browser_id] = entry
        logger.info(
            "Provisioned K8s browser %s for session %s (pod %s, service %s)",
            browser_id, session_id, pod_name, service_name,
        )
        return browser_id, endpoint

    async def _wait_for_ready(self, api: client.CoreV1Api, pod_name: str) -> None:
        """Watch the pod until it has a Ready condition or timeout."""
        w = watch.Watch()
        try:
            async with asyncio.timeout(self._pod_ready_timeout):
                async for event in w.stream(
                    api.list_namespaced_pod,
                    namespace=self._namespace,
                    field_selector=f"metadata.name={pod_name}",
                    timeout_seconds=self._pod_ready_timeout,
                ):
                    pod = event["object"]
                    if self._is_pod_ready(pod):
                        return
                    phase = pod.status.phase if pod.status else "Unknown"
                    if phase in ("Failed", "Succeeded"):
                        raise RuntimeError(
                            f"Browser pod {pod_name} entered {phase} phase",
                        )
        except TimeoutError:
            raise RuntimeError(
                f"Browser pod {pod_name} did not become ready within "
                f"{self._pod_ready_timeout}s",
            )
        finally:
            w.stop()

    async def _delete_pod_safe(self, api: client.CoreV1Api, pod_name: str) -> None:
        try:
            await api.delete_namespaced_pod(
                pod_name, self._namespace, grace_period_seconds=5,
            )
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete pod %s: %s", pod_name, exc)

    async def _delete_service_safe(
        self, api: client.CoreV1Api, service_name: str,
    ) -> None:
        try:
            await api.delete_namespaced_service(service_name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete service %s: %s", service_name, exc)

    @staticmethod
    def _is_pod_ready(pod: client.V1Pod) -> bool:
        if not pod.status or not pod.status.conditions:
            return False
        return any(
            c.type == "Ready" and c.status == "True"
            for c in pod.status.conditions
        )
```

> Note on the protocol mismatch: after Task 4, direct calls to
> `K8sBrowserBackend.provision(..., session_id=..., org_id=..., user_id=...)`
> work, but `BrowserPool` still calls the Phase A protocol
> `backend.provision(spec)`. Do not wire this backend into the worker until
> Task 5 is complete.

- [ ] **Step 4: Run** — `uv run pytest tests/test_browser_kubernetes.py::TestProvision -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/kubernetes.py tests/test_browser_kubernetes.py
git commit -m "feat(browser): provision K8s pod + service with rollback on failure"
```

---

## Task 5: Align the `BrowserBackend` protocol — pass session/org/user IDs

**Files:**
- Modify: `surogates/browser/base.py`
- Modify: `surogates/browser/process.py` (accept the new kwargs as no-ops)
- Modify: `surogates/browser/pool.py` (forward the new kwargs)
- Modify: `tests/test_browser_pool.py` (FakeBackend signature)
- Modify: `tests/test_browser_process.py` (call provision with kwargs)

The K8s backend needs `session_id`, `org_id`, `user_id` to label the pod
and Service. The Process backend doesn't need them (containers aren't
labelled per session — port allocation is the only multiplexer). To keep
one protocol, we extend `BrowserBackend.provision` with these kwargs and
let Process ignore them.

- [ ] **Step 1: Write the failing test** — append to `tests/test_browser_kubernetes.py`:

```python
class TestProtocolAlignment:
    async def test_pool_forwards_session_to_k8s_provision(
        self, backend: K8sBrowserBackend, monkeypatch
    ) -> None:
        from surogates.browser.pool import BrowserPool
        from surogates.browser.registry import BrowserEntry

        api = MagicMock()
        api.create_namespaced_pod = AsyncMock()
        api.create_namespaced_service = AsyncMock()

        async def fake_get_api() -> "MagicMock":
            return api

        async def fake_wait_ready(api_inner, pod_name: str) -> None:
            return None

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        monkeypatch.setattr(backend, "_wait_for_ready", fake_wait_ready)

        class FakeRegistry:
            def __init__(self) -> None:
                self.entries: dict[str, BrowserEntry] = {}

            async def set(self, entry: BrowserEntry) -> None:
                self.entries[entry.session_id] = entry

            async def get(self, session_id: str) -> BrowserEntry | None:
                return self.entries.get(session_id)

            async def delete(self, session_id: str) -> None:
                self.entries.pop(session_id, None)

        pool = BrowserPool(backend=backend, registry=FakeRegistry())  # type: ignore[arg-type]
        await pool.ensure(
            session_id="sess-7", org_id="org-7", user_id="user-7",
            spec=BrowserSpec(),
        )
        # Inspect the pod manifest that was sent to K8s.
        pod_arg = api.create_namespaced_pod.call_args.args[1]
        assert pod_arg.metadata.labels["surogates.ai/session-id"] == "sess-7"
        assert pod_arg.metadata.labels["surogates.ai/org-id"] == "org-7"
        assert pod_arg.metadata.labels["surogates.ai/user-id"] == "user-7"
```

- [ ] **Step 2: Run** — FAIL because `BrowserPool.ensure` doesn't currently
forward `session_id`/`org_id`/`user_id` to `backend.provision` — the Phase A
pool calls `backend.provision(spec)` with no extra kwargs.

- [ ] **Step 3: Update the protocol**

Edit `surogates/browser/base.py`. Replace the `BrowserBackend.provision`
signature:

```python
class BrowserBackend(Protocol):
    async def provision(
        self,
        spec: BrowserSpec,
        *,
        session_id: str,
        org_id: str,
        user_id: str,
    ) -> tuple[str, BrowserEndpoint]:
        """Create a browser instance.

        ``session_id`` / ``org_id`` / ``user_id`` are used by backends
        that label their resources (K8s); the process backend ignores
        them. Returns ``(browser_id, endpoint)`` once the instance is
        ready to accept REST calls.
        """
        ...

    async def status(self, browser_id: str) -> BrowserStatus: ...
    async def destroy(self, browser_id: str) -> None: ...
```

- [ ] **Step 4: Update `ProcessBrowserBackend.provision`** in
`surogates/browser/process.py` — accept and ignore the new kwargs:

```python
    async def provision(
        self,
        spec: BrowserSpec,
        *,
        session_id: str = "",
        org_id: str = "",
        user_id: str = "",
    ) -> tuple[str, BrowserEndpoint]:
        # session_id / org_id / user_id are unused for the local docker
        # backend — there's nothing to label. Accepted for protocol parity.
        ...   # rest of the existing body
```

- [ ] **Step 5: Update `BrowserPool.ensure`** in
`surogates/browser/pool.py` — forward the kwargs:

```python
        browser_id, endpoint = await self._backend.provision(
            spec,
            session_id=session_id,
            org_id=org_id,
            user_id=user_id,
        )
```

- [ ] **Step 6: Update existing tests**

In `tests/test_browser_pool.py`'s `FakeBackend.provision`, accept the
new kwargs:

```python
    async def provision(
        self, spec: BrowserSpec, *, session_id: str = "", org_id: str = "", user_id: str = "",
    ) -> tuple[str, BrowserEndpoint]:
        # ...rest unchanged
```

In `tests/test_browser_process.py`, where `await backend.provision(BrowserSpec(...))`
appears, change to `await backend.provision(BrowserSpec(...), session_id="t", org_id="t", user_id="t")`
on every call.

- [ ] **Step 7: Run all browser tests**

```bash
uv run pytest tests/test_browser_pool.py tests/test_browser_process.py tests/test_browser_kubernetes.py -v
```

Expected: all PASS, including the new `TestProtocolAlignment::test_pool_forwards_session_to_k8s_provision`.

- [ ] **Step 8: Commit**

```bash
git add surogates/browser/base.py surogates/browser/process.py \
        surogates/browser/pool.py tests/test_browser_pool.py \
        tests/test_browser_process.py tests/test_browser_kubernetes.py
git commit -m "refactor(browser): pass session/org/user ids through provision protocol"
```

---

## Task 6: `status()` — read pod, map K8s phase to `BrowserStatus`

**Files:**
- Modify: `surogates/browser/kubernetes.py`
- Test: `tests/test_browser_kubernetes.py` (extend)

- [ ] **Step 1: Write the failing test** — append:

```python
class TestStatus:
    async def test_status_running(self, backend: K8sBrowserBackend) -> None:
        # Seed a known entry.
        backend._pods["bid"] = MagicMock()
        backend._pods["bid"].pod_name = "browser-bid"
        backend._pods["bid"].namespace = "test-ns"
        backend._pods["bid"].status = BrowserStatus.RUNNING

        api = MagicMock()
        running_pod = MagicMock()
        running_pod.status.phase = "Running"
        running_pod.status.conditions = [
            MagicMock(type="Ready", status="True"),
        ]
        api.read_namespaced_pod = AsyncMock(return_value=running_pod)

        async def fake_get_api() -> "MagicMock":
            return api

        backend._get_api = fake_get_api  # type: ignore[assignment]
        assert await backend.status("bid") == BrowserStatus.RUNNING

    async def test_status_pending_when_phase_pending(self, backend: K8sBrowserBackend) -> None:
        backend._pods["bid"] = MagicMock(pod_name="browser-bid", namespace="test-ns")

        api = MagicMock()
        pending_pod = MagicMock()
        pending_pod.status.phase = "Pending"
        pending_pod.status.conditions = []
        api.read_namespaced_pod = AsyncMock(return_value=pending_pod)

        async def fake_get_api() -> "MagicMock":
            return api

        backend._get_api = fake_get_api  # type: ignore[assignment]
        assert await backend.status("bid") == BrowserStatus.PENDING

    async def test_status_terminated_when_pod_404(self, backend: K8sBrowserBackend) -> None:
        backend._pods["bid"] = MagicMock(pod_name="browser-bid", namespace="test-ns")
        api = MagicMock()
        api.read_namespaced_pod = AsyncMock(
            side_effect=ApiException(status=404),
        )

        async def fake_get_api() -> "MagicMock":
            return api

        backend._get_api = fake_get_api  # type: ignore[assignment]
        assert await backend.status("bid") == BrowserStatus.TERMINATED
        assert "bid" not in backend._pods  # cache is cleaned up

    async def test_status_unknown_returns_terminated(self, backend: K8sBrowserBackend) -> None:
        # Never provisioned.
        assert await backend.status("never") == BrowserStatus.TERMINATED
```

- [ ] **Step 2: Run** — 4 FAIL.

- [ ] **Step 3: Implement** — append:

```python
    async def status(self, browser_id: str) -> BrowserStatus:
        entry = self._pods.get(browser_id)
        if entry is None:
            return BrowserStatus.TERMINATED

        api = await self._get_api()
        try:
            pod = await api.read_namespaced_pod(entry.pod_name, self._namespace)
        except ApiException as exc:
            if exc.status == 404:
                self._pods.pop(browser_id, None)
                return BrowserStatus.TERMINATED
            logger.warning(
                "Status check for browser %s failed (HTTP %s); trusting cached %s",
                browser_id, exc.status, entry.status,
            )
            return entry.status

        new_status = self._map_pod_status(pod)
        entry.status = new_status
        return new_status

    @staticmethod
    def _map_pod_status(pod: client.V1Pod) -> BrowserStatus:
        if not pod.status:
            return BrowserStatus.PENDING
        phase = pod.status.phase
        if phase == "Running" and K8sBrowserBackend._is_pod_ready(pod):
            return BrowserStatus.RUNNING
        if phase == "Pending":
            return BrowserStatus.PENDING
        if phase in ("Failed", "Unknown"):
            return BrowserStatus.FAILED
        if phase == "Succeeded":
            return BrowserStatus.TERMINATED
        return BrowserStatus.PENDING
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/kubernetes.py tests/test_browser_kubernetes.py
git commit -m "feat(browser): map pod phase to BrowserStatus with 404 cache cleanup"
```

---

## Task 7: `destroy()` — idempotent service + pod deletion

**Files:**
- Modify: `surogates/browser/kubernetes.py`
- Test: `tests/test_browser_kubernetes.py` (extend)

- [ ] **Step 1: Write the failing test** — append:

```python
class TestDestroy:
    async def test_destroy_deletes_service_and_pod(
        self, backend: K8sBrowserBackend, monkeypatch
    ) -> None:
        from surogates.browser.kubernetes import _PodEntry as PE

        api = MagicMock()
        api.delete_namespaced_pod = AsyncMock()
        api.delete_namespaced_service = AsyncMock()

        async def fake_get_api() -> "MagicMock":
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        backend._pods["bid"] = PE(
            browser_id="bid", pod_name="browser-bid", service_name="browser-bid",
            namespace="test-ns", spec=BrowserSpec(),
            endpoint=BrowserEndpoint(rest_url="r", cdp_url="c", live_view_url="l"),
        )

        await backend.destroy("bid")
        assert api.delete_namespaced_pod.call_count == 1
        assert api.delete_namespaced_service.call_count == 1
        assert "bid" not in backend._pods

    async def test_destroy_unknown_is_noop(self, backend: K8sBrowserBackend) -> None:
        await backend.destroy("never")  # no raise

    async def test_destroy_swallows_404(
        self, backend: K8sBrowserBackend, monkeypatch
    ) -> None:
        from surogates.browser.kubernetes import _PodEntry as PE

        api = MagicMock()
        api.delete_namespaced_pod = AsyncMock(
            side_effect=ApiException(status=404),
        )
        api.delete_namespaced_service = AsyncMock(
            side_effect=ApiException(status=404),
        )

        async def fake_get_api() -> "MagicMock":
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        backend._pods["bid"] = PE(
            browser_id="bid", pod_name="browser-bid", service_name="browser-bid",
            namespace="test-ns", spec=BrowserSpec(),
            endpoint=BrowserEndpoint(rest_url="r", cdp_url="c", live_view_url="l"),
        )
        # Does not raise even though both deletions return 404.
        await backend.destroy("bid")
        assert "bid" not in backend._pods
```

- [ ] **Step 2: Run** — 3 FAIL.

- [ ] **Step 3: Implement** — append:

```python
    async def destroy(self, browser_id: str) -> None:
        entry = self._pods.pop(browser_id, None)
        if entry is None:
            return

        api = await self._get_api()
        # Service first so we stop new traffic before tearing down the pod.
        await self._delete_service_safe(api, entry.service_name)
        await self._delete_pod_safe(api, entry.pod_name)
        logger.info(
            "Destroyed K8s browser %s (pod %s, service %s)",
            browser_id, entry.pod_name, entry.service_name,
        )
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/kubernetes.py tests/test_browser_kubernetes.py
git commit -m "feat(browser): destroy service then pod, idempotent on 404"
```

---

## Task 8: `find_by_session()` — label-keyed pod lookup for Phase C fallback

**Files:**
- Modify: `surogates/browser/kubernetes.py`
- Test: `tests/test_browser_kubernetes.py` (extend)

The Phase C API server needs to resolve a session's browser pod even
when the Redis registry has lost the entry. This task adds the K8s-side
lookup: list pods by `surogates.ai/session-id={session_id}` and return
the endpoint reconstructed from the matching Service. Phase C composes
this with `BrowserRegistry` to produce the resolver; Phase B just exposes
the primitive.

- [ ] **Step 1: Write the failing test** — append:

```python
class TestFindBySession:
    async def test_find_returns_endpoint(
        self, backend: K8sBrowserBackend, monkeypatch
    ) -> None:
        api = MagicMock()
        # One pod with the requested session label.
        pod = MagicMock()
        pod.metadata.name = "browser-abcdef123456"
        pod.metadata.labels = {
            "app": "surogates-browser",
            "surogates.ai/browser-id": "abcdef1234567890",
            "surogates.ai/session-id": "sess-x",
            "surogates.ai/org-id": "org-x",
            "surogates.ai/user-id": "user-x",
        }
        api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[pod]))

        async def fake_get_api() -> "MagicMock":
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        result = await backend.find_by_session("sess-x")
        assert result is not None
        bid, endpoint = result
        assert bid == "abcdef1234567890"
        assert endpoint.rest_url == (
            "http://browser-abcdef123456.test-ns.svc:10001"
        )

    async def test_find_returns_none_when_no_match(
        self, backend: K8sBrowserBackend, monkeypatch
    ) -> None:
        api = MagicMock()
        api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[]))

        async def fake_get_api() -> "MagicMock":
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        assert await backend.find_by_session("sess-missing") is None

    async def test_find_uses_correct_label_selector(
        self, backend: K8sBrowserBackend, monkeypatch
    ) -> None:
        api = MagicMock()
        api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[]))

        async def fake_get_api() -> "MagicMock":
            return api

        monkeypatch.setattr(backend, "_get_api", fake_get_api)
        await backend.find_by_session("sess-y")
        kwargs = api.list_namespaced_pod.call_args.kwargs
        sel = kwargs.get("label_selector", "")
        assert "app=surogates-browser" in sel
        assert "surogates.ai/session-id=sess-y" in sel
```

- [ ] **Step 2: Run** — 3 FAIL.

- [ ] **Step 3: Implement** — append:

```python
    # ------------------------------------------------------------------
    # Cross-process resolver primitive (used by Phase C API server)
    # ------------------------------------------------------------------

    async def find_by_session(
        self, session_id: str,
    ) -> tuple[str, BrowserEndpoint] | None:
        """Resolve a session's browser via K8s labels (registry fallback).

        Returns ``(browser_id, BrowserEndpoint)`` if exactly one pod has
        ``surogates.ai/session-id={session_id}``; ``None`` otherwise.

        This is the K8s side of the Phase C ``BrowserResolver``: when
        Redis loses an entry (eviction, namespace flush), the API server
        rebuilds it from the cluster.
        """
        api = await self._get_api()
        selector = (
            f"app=surogates-browser,surogates.ai/session-id={session_id}"
        )
        result = await api.list_namespaced_pod(
            self._namespace, label_selector=selector,
        )
        items = list(getattr(result, "items", []) or [])
        if not items:
            return None
        pod = items[0]
        labels = (pod.metadata.labels or {})
        browser_id = labels.get("surogates.ai/browser-id", "")
        # The pod and service share a name, so we can rebuild URLs from
        # pod.metadata.name.
        service_name = pod.metadata.name
        endpoint = BrowserEndpoint(
            rest_url=f"http://{service_name}.{self._namespace}.svc:{SERVICE_PORT_REST}",
            cdp_url=f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_CDP}",
            live_view_url=f"ws://{service_name}.{self._namespace}.svc:{SERVICE_PORT_LIVE_VIEW}",
        )
        return browser_id, endpoint
```

- [ ] **Step 4: Run** — all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/kubernetes.py tests/test_browser_kubernetes.py
git commit -m "feat(browser): add K8s label-keyed find_by_session for API resolver fallback"
```

---

## Task 9: Worker bootstrap — switch on `browser.backend`

**Files:**
- Modify: `surogates/orchestrator/worker.py`

In Phase A the worker bootstrap raises if `browser.backend == "kubernetes"`
(see Phase A Task 19, Step 2). This task removes that guard and wires the
real backend.

- [ ] **Step 1: Locate the Phase A guard**

```bash
grep -n 'browser.backend == "kubernetes"' surogates/orchestrator/worker.py
```

You'll find the `raise RuntimeError("browser.backend=kubernetes is reserved for Phase B; ...")` block.

- [ ] **Step 2: Replace the guard with the real branch**

Phase A currently imports `BrowserControlStore`, `BrowserPool`,
`ProcessBrowserBackend`, and `BrowserRegistry` near the top of
`surogates/orchestrator/worker.py`. Keep those imports. Add the
`K8sBrowserBackend` import lazily inside the Kubernetes branch so workers
running in process mode do not need to initialize any Kubernetes client
state during import.

```python
    # Browser pool ---------------------------------------------------------
    if settings.browser.backend == "kubernetes":
        from surogates.browser.kubernetes import K8sBrowserBackend
        browser_backend = K8sBrowserBackend(
            namespace=settings.browser.k8s_namespace,
            service_account=settings.browser.k8s_service_account,
            pod_ready_timeout=settings.browser.pod_ready_timeout,
            image=settings.browser.image,
        )
    else:
        from surogates.browser.process import ProcessBrowserBackend
        browser_backend = ProcessBrowserBackend(
            image=settings.browser.image,
            rest_port_base=settings.browser.rest_port_base,
            cdp_port_base=settings.browser.cdp_port_base,
            live_view_port_base=settings.browser.live_view_port_base,
        )

    browser_registry = BrowserRegistry(redis_client)
    browser_control = BrowserControlStore(redis_client)
    # ... event_emitter and pool wiring stay as in Phase A.
```

- [ ] **Step 3: Run the broader test suite**

```bash
uv run pytest tests/ -k "browser or worker_bootstrap" -q
```

Expected: still green; no test attempts to actually create K8s resources
(integration test in Task 15 is opt-in).

- [ ] **Step 4: Commit**

```bash
git add surogates/orchestrator/worker.py
git commit -m "feat(browser): wire K8sBrowserBackend into worker bootstrap"
```

---

## Task 10: Custom container image — `images/browser/`

The browser image is a thin layer on top of kernel-images upstream:
pinned base SHA, `zstd` for Phase D profile sync, and Surogates labels.
Living at `ghcr.io/invergent-ai/surogates-agent-browser` keeps every
session-pod image in the project's own registry and lets us roll forward
without waiting on upstream `:stable` tag movement.

**Files:**
- Create: `images/browser/Dockerfile`
- Modify: `images/build.sh`
- Modify: `surogates/config.py` (bump `BrowserSettings.image` default)
- Modify: `surogates/browser/base.py` (bump `BrowserSpec.image` default)
- Modify: `surogates/browser/kubernetes.py` (bump `K8sBrowserBackend` constructor default)

- [ ] **Step 1: Look up a fresh kernel-images digest to pin**

```bash
docker pull ghcr.io/onkernel/chromium-headful:stable
docker inspect --format '{{index .RepoDigests 0}}' \
    ghcr.io/onkernel/chromium-headful:stable
```

Use the resulting `ghcr.io/onkernel/chromium-headful@sha256:...` string as
the `FROM` line. Pinning by digest (not tag) protects us from `:stable`
moving under us between deploys.

- [ ] **Step 2: Create the Dockerfile**

`images/browser/Dockerfile`:

```dockerfile
# Surogates Agent Browser — kernel-images-derived per-session browser pod.
#
# Minimal additions on top of upstream:
#   - zstd  (Phase D profile sync compression)
#   - Surogates OCI labels
#
# Build (from repo root):
#   docker build -t ghcr.io/invergent-ai/surogates-agent-browser:latest \
#       -f images/browser/Dockerfile .
# Or via the matrix: ./images/build.sh latest browser
#
# k3d/kind users can `kind load docker-image` after building.
ARG UPSTREAM=ghcr.io/onkernel/chromium-headful:stable
FROM ${UPSTREAM}

LABEL org.opencontainers.image.title="surogates-agent-browser" \
      org.opencontainers.image.description="Surogates per-session agent browser (kernel-images-derived)" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates" \
      org.opencontainers.image.base.name="ghcr.io/onkernel/chromium-headful"

# Phase D profile sync uses /fs/upload_zstd + /fs/download_dir_zstd; zstd
# must be present in the image for those endpoints to work.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends zstd \
    && rm -rf /var/lib/apt/lists/*

# Restore upstream's runtime user. Kernel-images ships entrypoint and
# default CMD; we don't override either, so the supervisor stack keeps
# its existing behaviour.
USER kernel
```

- [ ] **Step 3: Build the image locally and verify**

```bash
cd /work/surogates
docker pull ghcr.io/onkernel/chromium-headful:stable
UPSTREAM_DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' \
    ghcr.io/onkernel/chromium-headful:stable)"
sed -i "s|^ARG UPSTREAM=.*|ARG UPSTREAM=${UPSTREAM_DIGEST}|" \
    images/browser/Dockerfile
docker build -f images/browser/Dockerfile \
    -t ghcr.io/invergent-ai/surogates-agent-browser:latest .
docker run --rm \
    --entrypoint which \
    ghcr.io/invergent-ai/surogates-agent-browser:latest \
    zstd
```

Expected: `/usr/bin/zstd` printed.

- [ ] **Step 4: Add the image to `images/build.sh`**

Open `images/build.sh` and add `browser` to the `IMAGES` map alongside
the existing entries:

```bash
declare -A IMAGES=(
  [api]="surogates-api"
  [worker]="surogates-worker"
  [sandbox]="surogates-agent-sandbox"
  [s3fs]="surogates-s3fs"
  [browser]="surogates-agent-browser"
)
```

Also add a push guard so local verification does not require GHCR write
credentials:

```bash
PUSH="${PUSH:-1}"
```

and replace the unconditional push:

```bash
docker push "$full:latest"
```

with:

```bash
if [[ "$PUSH" == "1" ]]; then
  docker push "$full:latest"
fi
```

- [ ] **Step 5: Verify the matrix can build just the browser image**

```bash
PUSH=0 ./images/build.sh latest browser
```

Expected: only the browser image builds; no push is attempted. To publish,
run `./images/build.sh latest browser` with GHCR credentials configured.

- [ ] **Step 6: Bump the `BrowserSettings.image` default**

In `surogates/config.py`, find the `BrowserSettings` class (added in Phase
A Task 1) and change:

```python
    image: str = "ghcr.io/onkernel/chromium-headful:stable"
```

to:

```python
    image: str = "ghcr.io/invergent-ai/surogates-agent-browser:latest"
```

Also update:

- `surogates/browser/base.py` — `BrowserSpec.image`
- `surogates/browser/kubernetes.py` — `K8sBrowserBackend.__init__(image=...)`
- corresponding default assertions in
  `tests/test_browser_base.py::test_browser_settings_defaults` and
  `tests/test_browser_base.py::test_browser_spec_defaults`

```python
    assert s.image == "ghcr.io/invergent-ai/surogates-agent-browser:latest"
```

(There are two test assertions — `BrowserSettings` and `BrowserSpec` —
update both.)

- [ ] **Step 7: Run the foundation tests**

```bash
uv run pytest tests/test_browser_base.py tests/test_browser_kubernetes.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add images/browser/Dockerfile images/build.sh \
        surogates/config.py surogates/browser/base.py surogates/browser/kubernetes.py \
        tests/test_browser_base.py
git commit -m "feat(images): add surogates-agent-browser image + bump default"
```

---

## Task 11: Helm — `browser-rbac.yaml` (browser pod's ServiceAccount)

> **Both charts:** apply this change to **both**
> `helm/surogates/templates/` (this repo, `/work/surogates`) and
> `surogate_ops/agent_chart/templates/` (the surogate-ops repo,
> `/work/surogate-ops`). Two separate commits, one in each repo.

The browser pod runs untrusted Chromium driving arbitrary websites.
Like the existing sandbox SA, it gets **zero K8s API permissions** — a
ServiceAccount with no Role bound to it.

- [ ] **Step 1: Create the in-repo helm template**

Create `/work/surogates/helm/surogates/templates/browser-rbac.yaml`:

```yaml
{{- $fullname := include "surogates.fullname" . -}}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ $fullname }}-browser
  labels:
    {{- include "surogates.fullLabels" (dict "root" . "component" "browser") | nindent 4 }}
---
# No Role/RoleBinding — the browser ServiceAccount has zero permissions.
# The pod cannot list pods, read secrets, or interact with the K8s API.
# Same security posture as the sandbox SA (see sandbox-rbac.yaml).
```

- [ ] **Step 2: Verify the helm template renders**

```bash
helm template /work/surogates/helm/surogates --show-only templates/browser-rbac.yaml
```

Expected: a clean ServiceAccount manifest, no errors.

- [ ] **Step 3: Replicate to the surogate-ops chart**

```bash
cp /work/surogates/helm/surogates/templates/browser-rbac.yaml \
   /work/surogate-ops/surogate_ops/agent_chart/templates/browser-rbac.yaml
```

- [ ] **Step 4: Verify the second chart renders**

```bash
helm template /work/surogate-ops/surogate_ops/agent_chart \
    --show-only templates/browser-rbac.yaml
```

Expected: same clean ServiceAccount manifest.

- [ ] **Step 5: Commit in BOTH repos**

```bash
# Surogates repo
git -C /work/surogates add helm/surogates/templates/browser-rbac.yaml
git -C /work/surogates commit -m "chore(helm): add browser ServiceAccount (zero-permission)"

# Surogate-ops repo
git -C /work/surogate-ops add surogate_ops/agent_chart/templates/browser-rbac.yaml
git -C /work/surogate-ops commit -m "chore(helm): add browser ServiceAccount (zero-permission)"
```

---

## Task 12: Helm — `browser-networkpolicy.yaml`

> **Both charts.** Same parallel-update protocol as Task 11.

Browser pod ingress: from worker + api-server only. Egress: DNS + the
internet (Chromium needs to reach websites). This is the inverse of the
sandbox NetworkPolicy, which restricts egress to specific in-cluster
services — the browser legitimately needs to talk to the open internet.

- [ ] **Step 1: Create the in-repo template**

`/work/surogates/helm/surogates/templates/browser-networkpolicy.yaml`:

```yaml
{{- $fullname := include "surogates.fullname" . -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ $fullname }}-browser-isolation
  labels:
    {{- include "surogates.labels" . | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      app: surogates-browser
  policyTypes:
    - Egress
    - Ingress

  ingress:
    # REST + CDP from the worker.
    - from:
        - podSelector:
            matchLabels:
              {{- include "surogates.componentSelector" (dict "root" . "component" "worker") | nindent 14 }}
      ports:
        - protocol: TCP
          port: 10001
        - protocol: TCP
          port: 9222

    # Live view (NoVNC, port 6080 inside the pod) from the API server.
    # The API server proxies the WS through to the pod (Phase C).
    - from:
        - podSelector:
            matchLabels:
              {{- include "surogates.componentSelector" (dict "root" . "component" "api") | nindent 14 }}
      ports:
        - protocol: TCP
          port: 6080
        - protocol: TCP
          port: 10001

  egress:
    # DNS resolution.
    - to:
        - namespaceSelector: {}
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53

    # Open internet (Chromium needs to browse). Standard web ports.
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            {{- with .Values.browser.blockedCidrs }}
            except:
              {{- toYaml . | nindent 14 }}
            {{- end }}
      ports:
        - protocol: TCP
          port: 80
        - protocol: TCP
          port: 443
```

- [ ] **Step 2: Verify renders cleanly**

```bash
helm template /work/surogates/helm/surogates \
    --show-only templates/browser-networkpolicy.yaml
```

Expected: a NetworkPolicy whose `spec.podSelector.matchLabels` is exactly
`app: surogates-browser` (matching the Python-created pod labels), with
two ingress rules and two egress rules.

- [ ] **Step 3: Replicate to the surogate-ops chart**

```bash
cp /work/surogates/helm/surogates/templates/browser-networkpolicy.yaml \
   /work/surogate-ops/surogate_ops/agent_chart/templates/browser-networkpolicy.yaml

helm template /work/surogate-ops/surogate_ops/agent_chart \
    --show-only templates/browser-networkpolicy.yaml
```

- [ ] **Step 4: Commit in BOTH repos**

```bash
git -C /work/surogates add helm/surogates/templates/browser-networkpolicy.yaml
git -C /work/surogates commit -m "chore(helm): add browser NetworkPolicy (worker+api ingress, internet egress)"

git -C /work/surogate-ops add surogate_ops/agent_chart/templates/browser-networkpolicy.yaml
git -C /work/surogate-ops commit -m "chore(helm): add browser NetworkPolicy (worker+api ingress, internet egress)"
```

---

## Task 13: Helm — extend `worker-rbac.yaml` to allow browser Services

> **Both charts.** Same parallel-update protocol.

The worker creates browser pods AND browser services in the namespace.
The existing `worker-rbac.yaml` already grants `pods` (create/delete/get/
list/watch) and `pods/exec` (create/get) for the sandbox. We add
`services` (create/delete/get/list/watch).

- [ ] **Step 1: Locate the Role rules**

```bash
grep -n -A 20 "kind: Role" /work/surogates/helm/surogates/templates/worker-rbac.yaml
```

- [ ] **Step 2: Edit the in-repo template**

In `/work/surogates/helm/surogates/templates/worker-rbac.yaml`, append a
new rule after the existing `secrets` rule:

```yaml
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["create", "delete", "get", "list", "watch"]
```

- [ ] **Step 3: Verify renders**

```bash
helm template /work/surogates/helm/surogates --show-only templates/worker-rbac.yaml
```

Expected: the Role now has four rule blocks (pods, pods/exec, secrets, services).

- [ ] **Step 4: Mirror the change to the surogate-ops chart**

The two files were identical before Phase B, so a copy keeps them in sync:

```bash
cp /work/surogates/helm/surogates/templates/worker-rbac.yaml \
   /work/surogate-ops/surogate_ops/agent_chart/templates/worker-rbac.yaml

helm template /work/surogate-ops/surogate_ops/agent_chart --show-only templates/worker-rbac.yaml
```

- [ ] **Step 5: Commit in BOTH repos**

```bash
git -C /work/surogates add helm/surogates/templates/worker-rbac.yaml
git -C /work/surogates commit -m "chore(helm): allow worker to create/delete browser services"

git -C /work/surogate-ops add surogate_ops/agent_chart/templates/worker-rbac.yaml
git -C /work/surogate-ops commit -m "chore(helm): allow worker to create/delete browser services"
```

---

## Task 14: Helm — `values.yaml` and worker browser env vars

> **Both charts.** Apply each values and worker deployment change to both
> files. The browser pods are created by Python, so do **not** use Helm's
> `componentSelector` helper to select them; the NetworkPolicy from Task 12
> selects their literal `app: surogates-browser` label.

- [ ] **Step 1: Inspect current values**

```bash
grep -n -A 5 "sandbox:" /work/surogates/helm/surogates/values.yaml
```

You'll see the sandbox section with `image`, resource defaults, etc.
The browser section follows the same shape.

- [ ] **Step 2: Add the `browser:` block to in-repo `values.yaml`**

Append (alphabetical order roughly preserved):

```yaml
browser:
  # The browser is always enabled — there is no on/off switch. Backend
  # selects between the local docker dev path ("process") and the K8s
  # production path ("kubernetes").
  backend: kubernetes
  image: ghcr.io/invergent-ai/surogates-agent-browser:latest

  # Pod resource defaults (per-session). 1–2 GiB RAM is a reasonable
  # starting point for headful Chromium; tune up if pages OOM.
  resources:
    requests:
      cpu: "1"
      memory: "2Gi"
    limits:
      cpu: "2"
      memory: "4Gi"

  podReadyTimeout: 60
  activeDeadlineSeconds: 3600

  # K8s-specific (process backend ignores these).
  serviceAccountSuffix: browser   # → {fullname}-browser
  k8sNamespace: ""                # empty = release namespace
  blockedCidrs: []                # optional ipBlock.except entries for browser egress

  # Process backend (dev only). Ignored when backend=kubernetes.
  processPorts:
    restBase: 30000
    cdpBase: 31000
    liveViewBase: 32000
```

- [ ] **Step 3: Add browser env vars to the in-repo worker Deployment**

In `/work/surogates/helm/surogates/templates/worker-deployment.yaml`, add
these env vars immediately after the existing sandbox env block:

```yaml
            - name: SUROGATES_BROWSER_BACKEND
              value: {{ .Values.browser.backend | quote }}
            - name: SUROGATES_BROWSER_IMAGE
              value: {{ .Values.browser.image | quote }}
            - name: SUROGATES_BROWSER_CPU
              value: {{ .Values.browser.resources.requests.cpu | quote }}
            - name: SUROGATES_BROWSER_MEMORY
              value: {{ .Values.browser.resources.requests.memory | quote }}
            - name: SUROGATES_BROWSER_CPU_LIMIT
              value: {{ .Values.browser.resources.limits.cpu | quote }}
            - name: SUROGATES_BROWSER_MEMORY_LIMIT
              value: {{ .Values.browser.resources.limits.memory | quote }}
            - name: SUROGATES_BROWSER_POD_READY_TIMEOUT
              value: {{ .Values.browser.podReadyTimeout | quote }}
            - name: SUROGATES_BROWSER_ACTIVE_DEADLINE_SECONDS
              value: {{ .Values.browser.activeDeadlineSeconds | quote }}
            {{- if eq .Values.browser.backend "kubernetes" }}
            - name: SUROGATES_BROWSER_K8S_NAMESPACE
              value: {{ default .Release.Namespace .Values.browser.k8sNamespace | quote }}
            - name: SUROGATES_BROWSER_K8S_SERVICE_ACCOUNT
              value: {{ printf "%s-%s" $fullname .Values.browser.serviceAccountSuffix | quote }}
            {{- else }}
            - name: SUROGATES_BROWSER_REST_PORT_BASE
              value: {{ .Values.browser.processPorts.restBase | quote }}
            - name: SUROGATES_BROWSER_CDP_PORT_BASE
              value: {{ .Values.browser.processPorts.cdpBase | quote }}
            - name: SUROGATES_BROWSER_LIVE_VIEW_PORT_BASE
              value: {{ .Values.browser.processPorts.liveViewBase | quote }}
            {{- end }}
```

Render the worker Deployment and verify the browser env block appears:

```bash
helm template /work/surogates/helm/surogates --show-only templates/worker-deployment.yaml \
  | grep -A 28 SUROGATES_BROWSER_BACKEND
```

- [ ] **Step 4: Verify browser NetworkPolicy label parity**

Render the NetworkPolicy and verify it selects the literal label written by
`K8sBrowserBackend._build_pod_manifest`:

```bash
helm template /work/surogates/helm/surogates --show-only templates/browser-networkpolicy.yaml \
  | grep -A 3 'podSelector:'
```

Expected:

```yaml
  podSelector:
    matchLabels:
      app: surogates-browser
```

- [ ] **Step 5: Replicate the values and worker env changes to the surogate-ops chart**

Open `/work/surogate-ops/surogate_ops/agent_chart/values.yaml` and add
the **same `browser:` block** in the same position. Use diff to confirm:

```bash
diff <(grep -A 30 '^browser:' /work/surogates/helm/surogates/values.yaml) \
     <(grep -A 30 '^browser:' /work/surogate-ops/surogate_ops/agent_chart/values.yaml)
```

Expected: empty (identical content).

Then apply the same `SUROGATES_BROWSER_*` env block to
`/work/surogate-ops/surogate_ops/agent_chart/templates/worker-deployment.yaml`
and render it:

```bash
helm template /work/surogate-ops/surogate_ops/agent_chart --show-only templates/worker-deployment.yaml \
  | grep -A 28 SUROGATES_BROWSER_BACKEND
```

- [ ] **Step 6: Commit in BOTH repos**

```bash
git -C /work/surogates add helm/surogates/values.yaml \
                          helm/surogates/templates/worker-deployment.yaml
git -C /work/surogates commit -m "chore(helm): add browser values and worker env"

git -C /work/surogate-ops add surogate_ops/agent_chart/values.yaml \
                              surogate_ops/agent_chart/templates/worker-deployment.yaml
git -C /work/surogate-ops commit -m "chore(helm): add browser values and worker env"
```

---

## Task 15: Opt-in integration test against a real K8s cluster

**Files:**
- Create: `tests/integration/test_browser_e2e_k8s.py`
- Modify: `pyproject.toml` (add `browser_e2e_k8s` marker)

- [ ] **Step 1: Add the marker** in `pyproject.toml` under
`[tool.pytest.ini_options]`. Preserve the existing Phase A marker and
default deselection; do not replace the whole `markers` array.

```toml
addopts = "-m 'not browser_e2e and not browser_e2e_k8s'"
markers = [
    "browser_e2e: end-to-end agent browser tests requiring Docker and the kernel-images image (opt-in)",
    "browser_e2e_k8s: end-to-end agent browser tests requiring kind/minikube and a K8s cluster (opt-in)",
]
```

- [ ] **Step 2: Write the test**

`tests/integration/test_browser_e2e_k8s.py`:

```python
"""End-to-end Phase B test against a real K8s cluster.

Setup before running:

    kind create cluster --name surogates-test
    PUSH=0 ./images/build.sh latest browser
    kind load docker-image \
        ghcr.io/invergent-ai/surogates-agent-browser:latest \
        --name surogates-test
    helm install surogates /work/surogates/helm/surogates \
        --namespace surogates --create-namespace \
        --set browser.backend=kubernetes \
        --set browser.image=ghcr.io/invergent-ai/surogates-agent-browser:latest

Run:

    uv run pytest -m browser_e2e_k8s tests/integration/test_browser_e2e_k8s.py -v

Skipped by default.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import httpx
import pytest

from surogates.browser.base import BrowserSpec
from surogates.browser.client import KernelBrowserClient
from surogates.browser.kubernetes import K8sBrowserBackend


pytestmark = pytest.mark.browser_e2e_k8s

NAMESPACE = os.environ.get("BROWSER_K8S_NAMESPACE", "surogates")
SERVICE_ACCOUNT = os.environ.get("BROWSER_K8S_SA", "surogates-browser")
LOCAL_REST_PORT = int(os.environ.get("BROWSER_K8S_LOCAL_REST_PORT", "39101"))
IMAGE = os.environ.get(
    "BROWSER_E2E_IMAGE",
    "ghcr.io/invergent-ai/surogates-agent-browser:latest",
)


@pytest.fixture()
async def backend():
    yield K8sBrowserBackend(
        namespace=NAMESPACE,
        service_account=SERVICE_ACCOUNT,
        pod_ready_timeout=120,
        image=IMAGE,
    )


@pytest.fixture()
async def browser(backend):
    bid, endpoint = await backend.provision(
        BrowserSpec(image=IMAGE, pod_ready_timeout=120),
        session_id="e2e-session",
        org_id="e2e-org",
        user_id="e2e-user",
    )
    try:
        yield bid, endpoint
    finally:
        await backend.destroy(bid)


@pytest.fixture()
async def rest_url(backend, browser) -> AsyncIterator[str]:
    bid, _endpoint = browser
    service_name = backend._pods[bid].service_name
    proc = await asyncio.create_subprocess_exec(
        "kubectl",
        "-n",
        NAMESPACE,
        "port-forward",
        f"svc/{service_name}",
        f"{LOCAL_REST_PORT}:10001",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with httpx.AsyncClient(timeout=1.0) as http:
            for _ in range(120):
                if proc.returncode is not None:
                    stderr = (await proc.stderr.read()).decode(errors="replace")
                    raise RuntimeError(f"kubectl port-forward exited: {stderr}")
                try:
                    resp = await http.get(f"http://127.0.0.1:{LOCAL_REST_PORT}/spec.json")
                    if resp.status_code == 200:
                        break
                except Exception:
                    await asyncio.sleep(0.25)
            else:
                raise RuntimeError("port-forward did not become ready")

        yield f"http://127.0.0.1:{LOCAL_REST_PORT}"
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def test_provision_creates_running_pod(backend, browser) -> None:
    bid, _endpoint = browser
    from surogates.browser.base import BrowserStatus
    assert await backend.status(bid) == BrowserStatus.RUNNING


async def test_navigate_through_port_forward(rest_url) -> None:
    # ClusterIP service DNS is not reachable from a local pytest process,
    # so the e2e test uses kubectl port-forward to reach the REST service.
    async with KernelBrowserClient(rest_url=rest_url) as c:
        out = await c.navigate("https://example.com")
        assert "Example" in out["title"]


async def test_find_by_session_returns_endpoint(backend, browser) -> None:
    _bid, _endpoint = browser
    found = await backend.find_by_session("e2e-session")
    assert found is not None
```

- [ ] **Step 3: Verify the K8s e2e is skipped by default**

```bash
uv run pytest tests/test_browser_kubernetes.py \
  tests/integration/test_browser_e2e_k8s.py -q
```

Expected: K8s unit tests pass and the opt-in real-cluster tests are
deselected by the default `addopts`. Do not use the full integration suite
as the only Phase B signal unless required environment config, including
`storage.bucket`, is set.

- [ ] **Step 4: Run the K8s e2e (optional, when a kind/minikube cluster is up)**

```bash
kind create cluster --name surogates-test
PUSH=0 ./images/build.sh latest browser
kind load docker-image \
    ghcr.io/invergent-ai/surogates-agent-browser:latest \
    --name surogates-test
helm install surogates /work/surogates/helm/surogates \
    --namespace surogates --create-namespace \
    --set browser.backend=kubernetes \
    --set browser.image=ghcr.io/invergent-ai/surogates-agent-browser:latest
uv run pytest -m browser_e2e_k8s tests/integration/test_browser_e2e_k8s.py -v -s
```

Expected: 3 tests PASS against the real cluster.

Cleanup:

```bash
kind delete cluster --name surogates-test
```

- [ ] **Step 5: Commit**

```bash
git -C /work/surogates add tests/integration/test_browser_e2e_k8s.py pyproject.toml
git -C /work/surogates commit -m "test(browser): add opt-in K8s e2e against real cluster"
```

---

## Final verification

After all 15 tasks:

```bash
# Focused Phase B verification
uv run pytest \
  tests/test_browser_base.py \
  tests/test_browser_pool.py \
  tests/test_browser_process.py \
  tests/test_browser_kubernetes.py \
  tests/test_browser_tools.py \
  tests/test_harness_resilience.py \
  tests/test_streaming_executor.py \
  -q
```

Expected: green; new K8s backend tests counted alongside existing browser
and harness-dispatch coverage.

```bash
# Switch the worker config to K8s mode and confirm bootstrap doesn't break
SUROGATES_BROWSER_BACKEND=kubernetes uv run pytest \
  tests/test_browser_kubernetes.py tests/test_browser_tools.py -q
```

Expected: still green. Do not use the full integration suite as the only
Phase B signal unless the environment also sets required integration config
such as `storage.bucket`; otherwise unrelated feedback/session tests fail
before browser code is exercised.

Both helm charts render cleanly:

```bash
helm template /work/surogates/helm/surogates > /tmp/surogates-render.yaml
helm template /work/surogate-ops/surogate_ops/agent_chart > /tmp/agent-ops-render.yaml
diff <(grep '^kind:' /tmp/surogates-render.yaml | sort -u) \
     <(grep '^kind:' /tmp/agent-ops-render.yaml | sort -u)
```

Expected: empty diff (same set of resource kinds in both renders).

---

## What Phase B delivers

- **`K8sBrowserBackend`** — provisions per-session pods + per-session
  ClusterIP Services in K8s with discovery labels
  (`surogates.ai/session-id`, `/org-id`, `/user-id`).
- **Protocol alignment** — `BrowserBackend.provision` now takes
  session/org/user kwargs; `ProcessBrowserBackend` accepts them as
  no-ops, `K8sBrowserBackend` uses them to label resources.
- **`find_by_session` primitive** — Phase C's API server uses this
  for the stale-Redis fallback path; tested in isolation here.
- **Worker bootstrap** — switches between Process and K8s backends on
  `settings.browser.backend`. The `kubernetes` branch raises in Phase A;
  Phase B fills it in.
- **Custom image — `surogates-agent-browser`** — kernel-images-derived,
  pinned by digest, with `zstd` for Phase D profile sync. Lives at
  `images/browser/Dockerfile` alongside the other project images and
  participates in the `images/build.sh` matrix. The `BrowserSettings.image`
  default and helm `browser.image` value both point at it.
- **Helm: browser-rbac.yaml** — zero-permission ServiceAccount, mirrors
  sandbox-rbac.yaml.
- **Helm: browser-networkpolicy.yaml** — ingress from worker + api-server
  only, egress to DNS + open internet (Chromium needs to browse).
- **Helm: worker-rbac.yaml** — extended Role with `services` verbs so
  the worker can create/delete the per-session Service.
- **Helm: values.yaml** — `browser:` config block with K8s defaults.
- **Both helm charts updated in lockstep** — `helm/surogates/templates/`
  in this repo and `surogate_ops/agent_chart/templates/` in the
  surogate-ops repo, with parallel commits per repo.
- **Opt-in K8s e2e test** — verifies provision → status → navigate →
  find_by_session against a kind/minikube cluster.

Phase B explicitly does **not** ship: API server endpoints
(`/browser/state`, `/browser/live`, `/browser/control`), live-view
WebSocket proxy, SPA UI, profile sync, recording. Those are Phases C/D
and use the `find_by_session` primitive plus `BrowserRegistry` to compose
the full resolver.
