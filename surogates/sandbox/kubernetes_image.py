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
  finds it ready takes it over (its IP, the token derived from the deployment's
  secret and the session, checked); one starting is waited for; one that ended
  (or was made under another signing secret) is deleted and a new one made.
  Every delete names the pod's uid (a precondition), so it never takes a pod
  made since; an entry whose pod was replaced reads as gone.
- Labelled ``surogates.ai/session-image: "true"`` (an operator's
  NetworkPolicy can hold these pods to the egress their tasks may have) and
  ``surogates.ai/deployment`` (``sandbox.image_deployment``: the reaper's).
- The environment holds the token's digest only; seccomp RuntimeDefault.
- Its end: a turn's end and a worker's shutdown leave it.  A Stop or pause
  ends its commands (the executor's ``/stop``), its files kept; a pod whose
  executor does not answer is deleted.  :meth:`ImagePods.reap_images` deletes
  it, on any worker, once its session and the agents below it are at rest and
  its executor retires (``/retire``: nothing ran or was waited on for
  ``sandbox.image_idle_seconds``; atomic with its calls), or once it ended.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
#: How long a provision waits for the session's pod to be ready (a task image of several GB is pulled first).
_TAKE_OVER_S = 600.0
#: Waiting reasons of a container that will never start as it is.
_NEVER_STARTS = ("InvalidImageName", "ErrImageNeverPull", "CreateContainerConfigError", "CreateContainerError")
#: A pull that failed for good (its message), not one the kubelet retries with luck (a registry's 429, a timeout).
_PULL_REFUSED = ("not found", "manifest unknown", "unauthorized", "denied", "no such host")
#: How long a provision waits for the reaper to delete a pod whose executor it retired, before taking it back.
_RETIRED_WAIT_S = 20.0
#: How long a ready pod's executor may not answer before the provision gives up on it.
_NO_ANSWER_S = 60.0
#: Rounds at rest a pod's executor may not answer the reaper before it is deleted (hung, or another signing secret).
_UNANSWERED_ROUNDS = 5
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

    def _init_image_pods(self, *, bundle_image: str, deployment: str = "") -> None:
        self._bundle_image = bundle_image
        self._image_deployment = deployment
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

    # -- the pod as it is ---------------------------------------------------------------------------------------------

    async def _read_pod(self, api: Any, name: str) -> Any | None:
        try:
            return await api.read_namespaced_pod(name, self._namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise SandboxUnavailableError(f"Could not read the session's pod {name}: {exc}") from exc

    async def _force_delete_if_stuck(self, api: Any, pod: Any) -> None:
        if self._stuck_going(pod):
            # Its node is gone: the delete is forced (grace 0), still only for this uid.
            await self._delete_pod(api, pod.metadata.name, self._uid(pod))

    @staticmethod
    def _uid(pod: Any) -> str:
        return str(getattr(getattr(pod, "metadata", None), "uid", "") or "")

    @staticmethod
    def _terminating(pod: Any) -> bool:
        return bool(getattr(getattr(pod, "metadata", None), "deletion_timestamp", None))

    @staticmethod
    def _ended(pod: Any) -> bool:
        """Ended for good: its phase, or its sandbox container terminated (restartPolicy Never)."""
        status = getattr(pod, "status", None)
        if status is None:
            return False
        if status.phase in ("Failed", "Succeeded"):
            return True
        for c in status.container_statuses or []:
            if c.name == "sandbox" and c.state is not None and c.state.terminated is not None:
                return True
        return False

    @staticmethod
    def _why_not(pod: Any) -> str:
        """What its containers say (a pull refused, an init container's end), for the call's error."""
        status = getattr(pod, "status", None)
        said = []
        for c in [*(getattr(status, "init_container_statuses", None) or []),
                  *(getattr(status, "container_statuses", None) or [])]:
            state = c.state
            if state is not None and state.waiting is not None and state.waiting.reason:
                said.append(f"{c.name}: {state.waiting.reason} {state.waiting.message or ''}".strip())
            if state is not None and state.terminated is not None and state.terminated.exit_code:
                said.append(f"{c.name}: exit {state.terminated.exit_code} {state.terminated.reason or ''} "
                            f"{state.terminated.message or ''}".strip())
        return "; ".join(said)[:1000]

    @staticmethod
    def _never_starts(pod: Any) -> bool:
        status = getattr(pod, "status", None)
        for c in [*(getattr(status, "init_container_statuses", None) or []),
                  *(getattr(status, "container_statuses", None) or [])]:
            waiting = c.state.waiting if c.state is not None else None
            if waiting is None:
                continue
            if waiting.reason in _NEVER_STARTS:
                return True
            if waiting.reason in ("ErrImagePull", "ImagePullBackOff") and any(
                    word in (waiting.message or "").lower() for word in _PULL_REFUSED):
                return True
        return False

    @staticmethod
    def _stuck_going(pod: Any) -> bool:
        """Going for longer than its grace and a margin (a node lost: the kubelet never confirms)."""
        from datetime import datetime, timezone

        meta = getattr(pod, "metadata", None)
        since = getattr(meta, "deletion_timestamp", None)
        if not isinstance(since, datetime):
            return False
        grace = getattr(meta, "deletion_grace_period_seconds", None) or 0
        return (datetime.now(timezone.utc) - since).total_seconds() > grace + 30

    async def _delete_pod(self, api: Any, name: str, uid: str) -> bool:
        """Delete pod *name* only if it is still the one with *uid* (a precondition: never a pod made since).
        Whether it went (or was gone)."""
        body = client.V1DeleteOptions(grace_period_seconds=0, preconditions=client.V1Preconditions(uid=uid or None))
        try:
            await api.delete_namespaced_pod(name, self._namespace, body=body)
        except ApiException as exc:
            if exc.status == 404:
                return True
            if exc.status == 409:  # another pod by now
                return False
            raise SandboxUnavailableError(f"Could not delete the session's pod {name}: {exc}") from exc
        return True

    async def _executor_answers(self, ip: str, session: str) -> int | None:
        """The status an authenticated ``/activity`` gets from the pod's executor (``None``: no answer; 410: it is
        retired for the reaper)."""
        import httpx

        from surogates.sandbox.docker_image import session_token

        try:
            # trust_env=False: a proxy in the worker's environment never stands between it and a pod.
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as http:
                r = await http.get(f"http://{ip}:{self._executor_port}/activity",
                                   headers={"Authorization": f"Bearer {session_token(session)}"})
            if r.status_code == 200:
                try:
                    if (r.json() or {}).get("retired"):
                        return 410
                except ValueError:
                    pass
            return r.status_code
        except Exception:  # noqa: BLE001
            return None

    async def _executor_post(self, ip: str, session: str, path: str, *, timeout: float = 5.0,
                             body: dict | None = None) -> Any:
        """An authenticated POST to the pod's executor: the response, or the exception it raised."""
        import httpx

        from surogates.sandbox.docker_image import session_token

        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as http:
                return await http.post(f"http://{ip}:{self._executor_port}{path}", json=body,
                                       headers={"Authorization": f"Bearer {session_token(session)}"})
        except Exception as exc:  # noqa: BLE001
            return exc

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
        if spec.ssh_key_material:
            raise SandboxUnavailableError("SSH access is not offered in a session's own image", classification="config")
        from surogates.sandbox.docker_image import deployment_id
        from surogates.sandbox.executor_server import TOKEN_SHA256_ENV, token_sha256

        deployment = deployment_id(self._image_deployment)
        # Always the token's digest: the session's token outlives the pod (any worker derives it), and the pod's
        # spec is readable to whoever may get pods.
        token_var = client.V1EnvVar(name=TOKEN_SHA256_ENV, value=token_sha256(executor_token))
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
            # Set here, so an image that declares it lists nothing for the entry to clear.
            client.V1EnvVar(name="SUROGATES_IMAGE_ENV_UNSET", value=""),
        ]
        if self._mcp_proxy_url:
            env += self._mcp_env(spec, sandbox_id)
        set_here = {e.name for e in env}
        from surogates.sandbox.kubernetes import _DAEMON_SWITCHES

        # The daemon's own switches never come from a tenant's spec or a skill's passthrough.
        env += [client.V1EnvVar(name=k, value=v) for k, v in spec.env.items()
                if k not in set_here and k not in _DAEMON_SWITCHES]
        # The names the pod sets: the entry clears the image's control names but these (the pod's own values).
        env.append(client.V1EnvVar(name="SUROGATES_IMAGE_ENV_SET", value=",".join(e.name for e in env)))
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
                        "surogates.ai/session-image": "true", "surogates.ai/deployment": deployment},
                annotations={"surogates.ai/created-at": datetime.now(timezone.utc).isoformat(),
                             "surogates.ai/session": spec.session_id or ""},
            ),
            spec=client.V1PodSpec(
                service_account_name=self._service_account,
                automount_service_account_token=False,
                enable_service_links=False,
                # The executor's /stop ends every process it may signal but PID 1: its own namespace only.
                share_process_namespace=False,
                # No active deadline: the session's work lives as long as the session needs it; the reaper ends it.
                restart_policy="Never",
                security_context=client.V1PodSecurityContext(
                    seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                ),
                init_containers=init,
                containers=[sandbox],
                volumes=[
                    client.V1Volume(name="surogates-executor", empty_dir=client.V1EmptyDirVolumeSource()),
                    client.V1Volume(name="surogates-state",
                                    empty_dir=client.V1EmptyDirVolumeSource(medium="Memory", size_limit="1Mi")),
                ],
            ),
        )

    async def _provision_image_pod(self, spec: SandboxSpec, sandbox_id: str) -> str:
        from surogates.sandbox.docker_image import session_token
        from surogates.sandbox.kubernetes import _PodEntry

        if not spec.session_id:
            raise SandboxUnavailableError("A session's own image needs its session", classification="config")
        api = await self._get_api()
        name = image_pod_name(spec, sandbox_id)
        # Derived from the deployment's signing secret and the session: any worker can call it (take-over).
        token = session_token(spec.session_id)
        manifest = self._image_pod_manifest(sandbox_id, name, spec, executor_token=token)
        loop = asyncio.get_running_loop()
        waits = max(float(self._pod_ready_timeout), _TAKE_OVER_S)
        deadline = loop.time() + waits
        made_uid = ""
        silent_since: float | None = None
        retired_since: float | None = None
        why = ""
        while True:
            try:
                pod = await self._read_pod(api, name)
            except SandboxUnavailableError as exc:
                pod, why = False, str(exc)  # the API unsure (throttled, a blip): waited out like a pod starting
            if pod is None:
                try:
                    made = await api.create_namespaced_pod(self._namespace, manifest)
                    made_uid = self._uid(made)
                except ApiException as exc:
                    if exc.status != 409:  # 409: another worker made the session's pod just now (waited for)
                        raise SandboxUnavailableError(self._classify_create_pod_failure(exc)) from exc
            elif pod is not False:
                uid = self._uid(pod)
                if self._terminating(pod):
                    why = "it is being deleted"
                    await self._force_delete_if_stuck(api, pod)
                elif self._ended(pod) or self._never_starts(pod):
                    why = self._why_not(pod) or "it ended"
                    ours = bool(made_uid) and uid == made_uid
                    # Ended: what was in it is gone; one this provision made and that never came up is told as is.
                    await self._delete_pod(api, name, uid)
                    if ours:
                        raise SandboxUnavailableError(f"The session's pod {name} did not start: {why}",
                                                      classification="readiness")
                    logger.warning("The pod %s of session %s ended (%s): a new one starts", name, spec.session_id,
                                   why)
                elif self._is_pod_ready(pod) and pod.status.pod_ip:
                    ip = pod.status.pod_ip
                    answered = await self._executor_answers(ip, spec.session_id)
                    if answered == 401:
                        # Made under another signing secret (rotated): its executor never takes this deployment's
                        # token; replaced (its files go with it: a pod cannot be set aside).
                        logger.warning("The pod %s of session %s refuses this deployment's token: replaced", name,
                                       spec.session_id)
                        await self._delete_pod(api, name, uid)
                    elif answered == 410:
                        # Retired for the reaper: deleted in a moment; still there after the wait, the reaper kept
                        # it, and it is taken back (its files as they are).
                        retired_since = retired_since if retired_since is not None else loop.time()
                        why = "its executor is retired for the reaper"
                        if loop.time() - retired_since > _RETIRED_WAIT_S:
                            await self._executor_post(ip, spec.session_id, "/unretire")
                            retired_since = None
                    elif answered is not None:
                        entry = _PodEntry(sandbox_id=sandbox_id, pod_name=name, secret_name="",
                                          namespace=self._namespace, spec=spec, pod_ip=ip, token=token,
                                          status=SandboxStatus.RUNNING)
                        entry.uid = uid
                        entry.status_checked_at = time.monotonic()
                        self._pods[sandbox_id] = entry
                        if made_uid and uid == made_uid:
                            self._created_pods.add(sandbox_id)
                        logger.info("Sandbox %s in the pod %s of session %s (%s)", sandbox_id, name,
                                    spec.session_id, "made" if uid == made_uid else "taken over")
                        return sandbox_id
                    else:
                        silent_since = silent_since if silent_since is not None else loop.time()
                        why = f"its executor at {ip}:{self._executor_port} does not answer"
                        if loop.time() - silent_since > _NO_ANSWER_S:
                            raise SandboxUnavailableError(f"The session's pod {name} is ready but {why}",
                                                          classification="readiness")
                else:
                    why = self._why_not(pod) or "starting"
            if loop.time() > deadline:
                # Kept: another worker may be bringing it up, or working in it.
                raise SandboxUnavailableError(
                    f"The session's pod {name} was not ready within {waits:.0f}s: {why}", classification="readiness",
                )
            await asyncio.sleep(1.0)

    # -- its end -----------------------------------------------------------------------------------------------------

    async def _stop_image_pod(self, api: Any, name: str) -> None:
        """A Stop of the session in pod *name*: its commands end (the executor's ``/stop``), its files stay.  Only a
        pod whose executor cannot be reached at all, twice, or that refuses this deployment's token, is deleted; a
        slow or odd answer keeps it (logged)."""
        import httpx

        pod = await self._read_pod(api, name)
        if pod is None or self._terminating(pod):
            return
        session = ((pod.metadata.annotations or {}) if pod.metadata else {}).get("surogates.ai/session") or ""
        ip = pod.status.pod_ip if pod.status else ""
        if ip and session and not self._ended(pod):
            for timeout in (10.0, 30.0):
                answer = await self._executor_post(ip, session, "/stop", timeout=timeout)
                if isinstance(answer, httpx.Response):
                    if answer.status_code == 200:
                        logger.info("Stopped the commands in the pod %s (kept)", name)
                        return
                    if answer.status_code != 401:
                        logger.warning("The pod %s answered its Stop with %s: kept", name, answer.status_code)
                        return
                    break  # another signing secret: never this deployment's to stop
                if not isinstance(answer, (httpx.ConnectError, httpx.ConnectTimeout)):
                    logger.warning("The pod %s did not take its Stop (%s): kept", name, type(answer).__name__)
                    return
        await self._delete_pod(api, name, self._uid(pod))
        logger.info("Deleted the pod %s: its executor could not be reached for the Stop", name)

    async def stop_image_of_session(self, session_id: str) -> None:
        """A Stop, a pause or ``/clear`` of a session in its own image, on any worker: its commands end, its files
        stay for its next call."""
        api = await self._get_api()
        await self._stop_image_pod(api, image_pod_name(SandboxSpec(session_id=session_id), session_id))

    async def gone(self, sandbox_id: str) -> bool:
        """Whether *sandbox_id*'s pod no longer serves: deleted, replaced (another uid), going, or ended.  An API
        error is no answer."""
        entry = self._pods.get(sandbox_id)
        if entry is None:
            return True
        api = await self._get_api()
        try:
            pod = await self._read_pod(api, entry.pod_name)
        except SandboxUnavailableError:
            return False
        return (pod is None or self._uid(pod) != getattr(entry, "uid", "") or self._terminating(pod)
                or self._ended(pod))

    async def reap_images(self, at_rest: Any, *, idle_seconds: float) -> list[tuple[str, str]]:
        """Delete this deployment's session-image pods whose session (and the agents below it) is at rest and whose
        executor retires (``/retire``: unused for *idle_seconds* by its own clock, nothing running or waited on;
        atomic with its calls), or that ended, or that never came to run, or whose executor did not answer for a
        few rounds at rest (hung, or another signing secret).  Unsure: kept.  (session, pod name) of each one."""
        import httpx

        from surogates.sandbox.docker_image import deployment_id

        if not self._bundle_image or not self._image_deployment:
            return []  # no session image can run here
        api = await self._get_api()
        selector = ("surogates.ai/session-image=true,"
                    f"surogates.ai/deployment={deployment_id(self._image_deployment)}")
        try:
            pods = await api.list_namespaced_pod(self._namespace, label_selector=selector)
        except ApiException:
            return []
        unanswered = self.__dict__.setdefault("_unanswered_pods", {})
        listed = {self._uid(p) for p in pods.items or []}
        for uid in [u for u in unanswered if u not in listed]:
            unanswered.pop(uid, None)
        reaped: list[tuple[str, str]] = []
        for pod in pods.items or []:
            name, uid = pod.metadata.name, self._uid(pod)
            session = ((pod.metadata.annotations or {}) if pod.metadata else {}).get("surogates.ai/session") or ""
            try:
                if self._terminating(pod):
                    await self._force_delete_if_stuck(api, pod)
                    continue
                if not session or not await at_rest(session):
                    unanswered.pop(uid, None)
                    continue
                if not self._ended(pod):
                    ip = (pod.status.pod_ip if pod.status else "") or ""
                    if not ip or (pod.status.phase if pod.status else "") != "Running":
                        if not self._old(pod):
                            continue  # starting: its provision waits for it
                    else:
                        answer = await self._executor_post(ip, session, "/retire", body={"min_idle_seconds":
                                                                                         idle_seconds})
                        seen = None
                        if isinstance(answer, httpx.Response) and answer.status_code == 200:
                            try:
                                seen = answer.json()
                            except ValueError:
                                seen = None
                        if not isinstance(seen, dict):
                            unanswered[uid] = unanswered.get(uid, 0) + 1
                            if unanswered[uid] < _UNANSWERED_ROUNDS:
                                continue
                        elif not seen.get("retired"):
                            unanswered.pop(uid, None)
                            continue
                if not await self._delete_pod(api, name, uid):
                    continue
                unanswered.pop(uid, None)
            except Exception:  # noqa: BLE001 - one pod's trouble never ends the round; unsure: kept
                logger.debug("Could not reap the session-image pod %s", name, exc_info=True)
                continue
            for sid, entry in list(self._pods.items()):
                if entry.pod_name == name:
                    self.forget(sid)
            logger.info("Reaped the pod %s of session %s at rest", name, session)
            reaped.append((session, name))
        return reaped

    @staticmethod
    def _old(pod: Any) -> bool:
        """Made longer ago than a provision waits for it (never came to run: unschedulable, a pull left behind)."""
        from datetime import datetime, timezone

        made = getattr(getattr(pod, "metadata", None), "creation_timestamp", None)
        return isinstance(made, datetime) and (datetime.now(timezone.utc) - made).total_seconds() > _TAKE_OVER_S
