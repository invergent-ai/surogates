"""The Kubernetes backend's pods in a session's own image (session config ``sandbox_image``).

As on Docker (``docker_image``): the task's image runs the tool executor of the
executor bundle (``images/executor-bundle``), in place of its entrypoint, as
the image's own user, in the session's folder of the image; and it is one pod
per session, whichever worker asks for it.

- The bundle reaches the pod through an init container from the bundle image
  that copies ``/opt/surogates`` into an emptyDir, mounted read-only at
  ``/opt/surogates`` in the sandbox container (no cluster feature needed; an
  ImageVolume would spare the copy where the cluster has it).
- The image's own environment is unknown to the worker (it never pulls the
  image): a second init container, the task's image with the bundle's Python,
  writes it to a file (``SUROGATES_IMAGE_ENV_FILE``), from which the commands
  get it; the executor's start (``surogates.image_entry``) clears the image's
  values of the executor's control names from its own environment.
- No s3fs sidecar and no storage secret: the workspace is the image's folder.
  No service account token and no service links: the pod needs no cluster API.
- The pod is named after the session (``sbx-img-<session>``): a worker that
  finds it ready takes it over (its IP and token from the pod's spec); one that
  ended is deleted and a new one made; a create that finds the name taken waits
  for that pod and takes it over.
- Labelled ``surogates.ai/session-image: "true"``, so an operator's
  NetworkPolicy can hold these pods to the egress their tasks may have.
- Its end: a turn's end leaves it (the session's config says so on every
  worker), a worker's shutdown leaves it; :meth:`ImagePods.reap_images` deletes
  it, on any worker, once its session and the agents below it are at rest and
  its executor retires (``/retire``: nothing ran or was waited on for
  ``sandbox.image_idle_seconds``; atomic with its calls).  A Stop or pause
  deletes it (:meth:`ImagePods.stop_image_of_session`): a pod cannot stop and
  start again, so there its files go with it (Docker keeps them), and the
  session's next call is told.  Its active deadline is a day
  (``_IMAGE_POD_DEADLINE``), a bound for a pod no reaper takes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from surogates.sandbox.base import SandboxSpec, SandboxStatus, SandboxUnavailableError
from surogates.sandbox.session_image import BUNDLE_MOUNT, BUNDLE_PYTHON, BUNDLE_TINI, valid_reference

logger = logging.getLogger(__name__)

STATE_MOUNT = "/surogates-state"
IMAGE_ENV_FILE = f"{STATE_MOUNT}/image-env.json"
#: The image's environment as the init container sees it, less what Kubernetes itself adds to every container.
_DUMP_ENV = (
    "import json, os\n"
    "skip = ('HOSTNAME',)\n"
    "env = {k: v for k, v in os.environ.items() if k not in skip and not k.startswith('KUBERNETES_')}\n"
    f"open({IMAGE_ENV_FILE!r}, 'w').write(json.dumps(env))\n"
)
_DROPPED_CAPS = ["NET_RAW", "MKNOD", "SETFCAP"]
_TAKE_OVER_S = 120.0
#: A session's pod outlives turns (the reaper ends it at rest): a day at most, not a turn sandbox's hour.
_IMAGE_POD_DEADLINE = 86400
_SMALL = client.V1ResourceRequirements(requests={"cpu": "100m", "memory": "64Mi"},
                                       limits={"cpu": "1", "memory": "256Mi"})


def image_pod_name(spec: SandboxSpec, sandbox_id: str) -> str:
    """The one pod of a session's own image (a session id is a UUID: 44 characters, a DNS label)."""
    return f"sbx-img-{(spec.session_id or sandbox_id).lower()}"[:63].rstrip("-")


class ImagePods:
    """The Kubernetes backend's part for sessions in their own image (mixed into ``K8sSandbox``)."""

    _namespace: str
    _executor_port: int
    _pods: dict
    _mcp_proxy_url: str

    def _init_image_pods(self, *, bundle_image: str) -> None:
        self._bundle_image = bundle_image
        #: The sandboxes whose pod a provision made new (not one taken over).
        self._created_pods: set[str] = set()

    def created(self, sandbox_id: str) -> bool:
        """Whether *sandbox_id*'s pod was made new from the image by its provision (not taken over)."""
        return sandbox_id in self._created_pods

    def forget(self, sandbox_id: str) -> None:
        """Drop the entry of a sandbox whose pod is not ready, without deleting the pod (the pool's cleanup): the
        provision that follows takes it over when it is ready, or replaces it when it ended."""
        self._pods.pop(sandbox_id, None)
        self._created_pods.discard(sandbox_id)

    async def stop_image_of_session(self, session_id: str) -> None:
        """A Stop, a pause or ``/clear`` of a session in its own image: its pod is deleted, wherever it was made (a
        pod cannot stop and start again: its commands and its files end with it; the next call makes a new one and
        the session is told)."""
        api = await self._get_api()
        name = image_pod_name(SandboxSpec(session_id=session_id), session_id)
        try:
            await api.delete_namespaced_pod(name, self._namespace, grace_period_seconds=0)
        except ApiException as exc:
            if exc.status != 404:
                raise SandboxUnavailableError(f"Could not delete the session's pod {name}: {exc}") from exc
        for sid, entry in list(self._pods.items()):
            if entry.pod_name == name:
                self.forget(sid)
        logger.info("Deleted the pod %s of session %s (a Stop)", name, session_id)

    def _image_pod_manifest(self, sandbox_id: str, pod_name: str, spec: SandboxSpec, *, executor_token: str) -> client.V1Pod:
        if not self._bundle_image or not valid_reference(self._bundle_image):
            raise SandboxUnavailableError(
                "A session's own image needs the executor bundle (sandbox.executor_bundle_image is not set)",
                classification="config",
            )
        if not spec.image_workdir:
            # The worker never pulls the image, so its WorkingDir is unknown here.
            raise SandboxUnavailableError(
                "On Kubernetes a session's own image needs its folder named (sandbox_workdir)", classification="config",
            )
        workdir = spec.image_workdir
        if getattr(self, "_token_env", "plain") == "sha256":
            from surogates.sandbox.executor_server import TOKEN_SHA256_ENV, token_sha256

            token_var = client.V1EnvVar(name=TOKEN_SHA256_ENV, value=token_sha256(executor_token))
        else:
            token_var = client.V1EnvVar(name="TOOL_EXECUTOR_TOKEN", value=executor_token)
        env = [
            client.V1EnvVar(name="WORKSPACE_DIR", value=workdir),
            client.V1EnvVar(name="TOOL_EXECUTOR_PORT", value=str(self._executor_port)),
            token_var,
            client.V1EnvVar(name="TOOL_EXECUTOR_REQUIRE_FUSE", value="0"),
            client.V1EnvVar(name="SUROGATES_IMAGE_SESSION", value="1"),
            client.V1EnvVar(name="SUROGATES_EXECUTOR_HOME", value=BUNDLE_MOUNT),
            # The image's control names whose value the pod leaves as the image's are cleared at the executor's start
            # (surogates.image_entry reads this file); the commands get the image's values from it.
            client.V1EnvVar(name="SUROGATES_IMAGE_ENV_FILE", value=IMAGE_ENV_FILE),
        ]
        if self._mcp_proxy_url:
            env += self._mcp_env(spec, sandbox_id)
        set_here = {e.name for e in env}
        from surogates.sandbox.kubernetes import _DAEMON_SWITCHES

        # The daemon's own switches never come from a tenant's spec or a skill's passthrough.
        env += [client.V1EnvVar(name=k, value=v) for k, v in spec.env.items()
                if k not in set_here and k not in _DAEMON_SWITCHES]
        bundle = client.V1VolumeMount(name="surogates-executor", mount_path=BUNDLE_MOUNT, read_only=True)
        state = client.V1VolumeMount(name="surogates-state", mount_path=STATE_MOUNT)
        restricted = client.V1SecurityContext(
            allow_privilege_escalation=False, capabilities=client.V1Capabilities(drop=list(_DROPPED_CAPS)),
        )
        init = [
            client.V1Container(
                name="executor-bundle", image=self._bundle_image,
                command=["cp", "-a", f"{BUNDLE_MOUNT}/.", "/bundle/"],
                volume_mounts=[client.V1VolumeMount(name="surogates-executor", mount_path="/bundle")],
                resources=_SMALL, security_context=restricted,
            ),
            client.V1Container(
                name="image-env", image=spec.session_image, working_dir=workdir,
                command=[BUNDLE_PYTHON, "-I", "-c", _DUMP_ENV],
                volume_mounts=[bundle, state], resources=_SMALL, security_context=restricted,
            ),
        ]
        sandbox = client.V1Container(
            name="sandbox", image=spec.session_image, working_dir=workdir,
            command=[BUNDLE_TINI, "--", BUNDLE_PYTHON, "-I", "-m", "surogates.image_entry"],
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/healthz", port=self._executor_port),
                period_seconds=1, timeout_seconds=2, failure_threshold=15,
            ),
            resources=client.V1ResourceRequirements(
                requests={"cpu": spec.cpu, "memory": spec.memory},
                limits={"cpu": spec.cpu_limit, "memory": spec.memory_limit},
            ),
            env=env, volume_mounts=[bundle, client.V1VolumeMount(name="surogates-state", mount_path=STATE_MOUNT,
                                                                  read_only=True)],
            security_context=restricted,
        )
        from datetime import datetime, timezone

        return client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name, namespace=self._namespace,
                labels={"app": "surogates-sandbox", "surogates.ai/sandbox-id": sandbox_id,
                        "surogates.ai/session-image": "true"},
                annotations={"surogates.ai/created-at": datetime.now(timezone.utc).isoformat(),
                             "surogates.ai/session": spec.session_id or ""},
            ),
            spec=client.V1PodSpec(
                service_account_name=self._service_account,
                automount_service_account_token=False,
                enable_service_links=False,
                active_deadline_seconds=_IMAGE_POD_DEADLINE,
                restart_policy="Never",
                init_containers=init,
                containers=[sandbox],
                volumes=[
                    client.V1Volume(name="surogates-executor", empty_dir=client.V1EmptyDirVolumeSource()),
                    client.V1Volume(name="surogates-state",
                                    empty_dir=client.V1EmptyDirVolumeSource(medium="Memory", size_limit="1Mi")),
                ],
            ),
        )

    async def _provision_image_pod(self, spec: SandboxSpec, sandbox_id: str, executor_token: str) -> str:
        from surogates.sandbox.kubernetes import _PodEntry

        from surogates.sandbox.docker_image import session_token

        if not spec.session_id:
            raise SandboxUnavailableError("A session's own image needs its session", classification="config")
        api = await self._get_api()
        name = image_pod_name(spec, sandbox_id)
        # Derived from the deployment's signing secret and the session: any worker can call it (take-over).
        executor_token = session_token(spec.session_id)
        manifest = self._image_pod_manifest(sandbox_id, name, spec, executor_token=executor_token)
        try:
            pod = await api.read_namespaced_pod(name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise SandboxUnavailableError(f"Could not read the session's pod {name}: {exc}") from exc
            pod = None
        if pod is not None and self._is_pod_ready(pod) and pod.status.pod_ip:
            return self._register_image_pod(spec, sandbox_id, name, pod.status.pod_ip, executor_token)
        if pod is not None:
            # Ended, or never to be ready: what was in it is gone, and a new one starts from the image.
            logger.warning("The pod %s of session %s is not ready: deleted, a new one starts", name, spec.session_id)
            await self._delete_and_wait(api, name)
        made = True
        try:
            await api.create_namespaced_pod(self._namespace, manifest)
        except ApiException as exc:
            if exc.status != 409:
                raise SandboxUnavailableError(self._classify_create_pod_failure(exc)) from exc
            # Another worker made the session's pod just now: taken over once ready.
            made = False
        entry = _PodEntry(sandbox_id=sandbox_id, pod_name=name, secret_name="", namespace=self._namespace, spec=spec,
                          token=executor_token)
        self._pods[sandbox_id] = entry
        try:
            await self._wait_for_ready(api, name, max(self._pod_ready_timeout, int(_TAKE_OVER_S)))
            pod = await api.read_namespaced_pod(name, self._namespace)
            entry.pod_ip = (pod.status.pod_ip if pod.status else "") or ""
            entry.token = executor_token
            if not entry.pod_ip:
                raise RuntimeError(f"Pod {name} has no IP after becoming ready")
            entry.status = SandboxStatus.RUNNING
            if made:
                self._created_pods.add(sandbox_id)
        except Exception as exc:
            logger.error("Sandbox pod %s failed to become ready", name, exc_info=True)
            await self._destroy_entry(api, entry)
            self._pods.pop(sandbox_id, None)
            raise SandboxUnavailableError(f"Sandbox pod {name} failed to become ready: {exc}") from exc
        logger.info("Provisioned K8s sandbox %s in the image %s (pod %s)", sandbox_id, spec.session_image, name)
        return sandbox_id

    def _register_image_pod(self, spec: SandboxSpec, sandbox_id: str, name: str, ip: str, token: str) -> str:
        from surogates.sandbox.kubernetes import _PodEntry

        entry = _PodEntry(sandbox_id=sandbox_id, pod_name=name, secret_name="", namespace=self._namespace, spec=spec,
                          pod_ip=ip, token=token, status=SandboxStatus.RUNNING)
        self._pods[sandbox_id] = entry
        logger.info("Took over the pod %s of session %s as sandbox %s", name, spec.session_id, sandbox_id)
        return sandbox_id

    async def _delete_and_wait(self, api: Any, name: str, seconds: float = 60.0) -> None:
        try:
            await api.delete_namespaced_pod(name, self._namespace, grace_period_seconds=0)
        except ApiException as exc:
            if exc.status != 404:
                raise SandboxUnavailableError(f"Could not delete the ended pod {name}: {exc}") from exc
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                await api.read_namespaced_pod(name, self._namespace)
            except ApiException as exc:
                if exc.status == 404:
                    return
            await asyncio.sleep(0.5)
        raise SandboxUnavailableError(f"The ended pod {name} was not gone within {seconds:g}s")

    async def reap_images(self, at_rest: Any, *, idle_seconds: float) -> list[str]:
        """Delete the session-image pods of this namespace whose session (and the agents below it) is at rest and
        whose executor went unused for *idle_seconds* by its own clock; one that ended, once its session rests.
        Unsure of either: kept.  Their session ids."""
        import httpx

        api = await self._get_api()
        try:
            pods = await api.list_namespaced_pod(self._namespace, label_selector="surogates.ai/session-image=true")
        except ApiException:
            return []
        reaped = []
        for pod in pods.items or []:
            session = ((pod.metadata.annotations or {}) if pod.metadata else {}).get("surogates.ai/session") or ""
            try:
                if not session or not await at_rest(session):
                    continue
            except Exception:  # noqa: BLE001 - unsure: kept
                continue
            phase = pod.status.phase if pod.status else ""
            if phase == "Running":
                from surogates.sandbox.docker_image import session_token

                ip = pod.status.pod_ip or ""
                if not ip:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        # Atomic with its calls: once retired, every call is refused, so the delete takes none.
                        r = await client.post(f"http://{ip}:{self._executor_port}/retire",
                                              headers={"Authorization": f"Bearer {session_token(session)}"},
                                              json={"min_idle_seconds": idle_seconds})
                    seen = r.json() if r.status_code == 200 else None
                except Exception:  # noqa: BLE001 - unsure: kept
                    seen = None
                if not isinstance(seen, dict) or not seen.get("retired"):
                    continue
            elif phase not in ("Failed", "Succeeded"):
                continue
            try:
                await api.delete_namespaced_pod(pod.metadata.name, self._namespace, grace_period_seconds=0)
            except ApiException as exc:
                if exc.status != 404:
                    continue
            for sid, entry in list(self._pods.items()):
                if entry.pod_name == pod.metadata.name:
                    self.forget(sid)
            logger.info("Reaped the pod %s of session %s at rest", pod.metadata.name, session)
            reaped.append(session)
        return reaped
