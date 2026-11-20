"""Kubernetes: a session's own image (session config ``sandbox_image``) runs in one pod per session.

The pod is the task's image with the executor bundle copied in by an init container; a second init container writes the
image's own environment for the commands; the sandbox container runs the bundle's executor as the image's user, in the
session's folder, with no storage sidecar, service account token or service links.  A worker that finds the session's
pod ready takes it over; one that ended is replaced.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client import ApiException

from surogates.sandbox.base import SandboxSpec, SandboxStatus, SandboxUnavailableError
from surogates.sandbox.kubernetes import K8sSandbox
from surogates.sandbox.kubernetes_image import IMAGE_ENV_FILE, image_pod_name

IMAGE = "registry.example/tasks/repo-fix:1"
BUNDLE = "registry.example/surogates-executor-bundle:test"
SESSION = "3f1c2a8e-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-signing-secret")


@pytest.fixture()
def k8s() -> K8sSandbox:
    return K8sSandbox(namespace="ns", service_account="sa", pod_ready_timeout=5, executor_port=8071,
                      storage_settings=MagicMock(endpoint="http://minio:9000", access_key="k", secret_key="s", region=""),
                      s3fs_image="s3fs:test", executor_bundle_image=BUNDLE)


def _spec(**kw) -> SandboxSpec:
    return SandboxSpec(session_id=SESSION, session_image=IMAGE, image_workdir="/app", env={"ORG_ID": "o"}, **kw)


def _env(container) -> dict[str, str]:
    return {e.name: e.value for e in container.env or []}


def test_the_pod_runs_the_bundles_executor_in_the_image(k8s):
    pod = k8s._image_pod_manifest("sbx1", image_pod_name(_spec(), "sbx1"), _spec(), executor_token="tok")
    spec = pod.spec
    [bundle, dump] = spec.init_containers
    [sandbox] = spec.containers
    assert bundle.image == BUNDLE and bundle.command == ["cp", "-a", "/opt/surogates/.", "/bundle/"]
    assert dump.image == IMAGE and dump.command[:2] == ["/opt/surogates/bin/python3", "-I"]
    assert IMAGE_ENV_FILE in dump.command[-1] and not dump.env
    assert sandbox.image == IMAGE and sandbox.working_dir == "/app"
    assert sandbox.command == ["/opt/surogates/bin/tini", "--", "/opt/surogates/bin/python3", "-I", "-m",
                               "surogates.image_entry"]
    env = _env(sandbox)
    assert env["WORKSPACE_DIR"] == "/app" and env["TOOL_EXECUTOR_TOKEN"] == "tok" and env["TOOL_EXECUTOR_PORT"] == "8071"
    assert env["TOOL_EXECUTOR_REQUIRE_FUSE"] == "0" and env["SUROGATES_IMAGE_SESSION"] == "1"
    assert env["SUROGATES_IMAGE_ENV_FILE"] == IMAGE_ENV_FILE and env["ORG_ID"] == "o"
    assert "PROJECT_DIR" not in env
    mounts = {m.mount_path: m for m in sandbox.volume_mounts}
    assert mounts["/opt/surogates"].read_only and mounts["/surogates-state"].read_only
    assert sandbox.security_context.allow_privilege_escalation is False
    assert sandbox.security_context.capabilities.drop == ["NET_RAW", "MKNOD", "SETFCAP"]
    assert sandbox.security_context.run_as_user is None  # the image's own user
    # No storage sidecar, no API token, no service links; labelled for an operator's NetworkPolicy.
    assert [c.name for c in spec.containers] == ["sandbox"]
    assert spec.automount_service_account_token is False and spec.enable_service_links is False
    assert pod.metadata.labels["surogates.ai/session-image"] == "true"
    assert pod.metadata.name == f"sbx-img-{SESSION}" and len(pod.metadata.name) <= 63
    # It outlives turns (the reaper ends it at rest): not a turn sandbox's hour.
    assert spec.active_deadline_seconds == 86400


def test_without_its_folder_or_the_bundle_it_is_refused(k8s):
    with pytest.raises(SandboxUnavailableError, match="sandbox_workdir"):
        k8s._image_pod_manifest("s", "p", SandboxSpec(session_id=SESSION, session_image=IMAGE), executor_token="t")
    k8s._bundle_image = ""
    with pytest.raises(SandboxUnavailableError, match="executor_bundle_image"):
        k8s._image_pod_manifest("s", "p", _spec(), executor_token="t")


def test_a_default_pod_is_what_it_was(k8s):
    """The MCP env moved into a helper: a default pod's manifest is unchanged (container, env, sidecar)."""
    k8s._mcp_proxy_url = "http://mcp:8000"
    with patch.dict(os.environ, {"SUROGATES_JWT_SECRET": "x" * 32}):
        pod = k8s._build_pod_manifest("0" * 32, "sandbox-0", "secret-0", SandboxSpec(env={"ORG_ID": str(SESSION)}),
                                      executor_token="tok")
    [sandbox, s3fs] = pod.spec.containers
    assert sandbox.command == ["tini", "--", "python", "-m", "surogates.sandbox.executor_server"]
    assert [e.name for e in sandbox.env] == ["WORKSPACE_DIR", "TOOL_EXECUTOR_PORT", "TOOL_EXECUTOR_TOKEN",
                                             "MCP_PROXY_URL", "MCP_PROXY_TOKEN", "ORG_ID"]
    assert s3fs.name == "s3fs" and pod.spec.init_containers is None


def _pod(ready: bool, token: str = "their-token", ip: str = "10.0.0.7", phase: str = "Running"):
    return SimpleNamespace(
        spec=SimpleNamespace(containers=[SimpleNamespace(name="sandbox", env=[SimpleNamespace(name="TOOL_EXECUTOR_TOKEN",
                                                                                                value=token)])]),
        status=SimpleNamespace(pod_ip=ip, phase=phase, conditions=[SimpleNamespace(type="Ready",
                                                                                   status="True" if ready else "False")],
                               container_statuses=[]),
    )


async def test_a_ready_pod_of_the_session_is_taken_over(k8s):
    api = MagicMock()
    api.read_namespaced_pod = AsyncMock(return_value=_pod(True))
    api.create_namespaced_pod = AsyncMock()
    k8s._api = api
    from surogates.sandbox.docker_image import session_token

    sid = await k8s.provision(_spec())
    entry = k8s._pods[sid]
    assert (entry.pod_ip, entry.token, entry.status, entry.secret_name) == ("10.0.0.7", session_token(SESSION),
                                                                           SandboxStatus.RUNNING, "")
    api.create_namespaced_pod.assert_not_awaited()


async def test_an_ended_pod_is_replaced_and_no_storage_secret_is_made(k8s):
    api = MagicMock()
    gone = ApiException(status=404)
    api.read_namespaced_pod = AsyncMock(side_effect=[_pod(False, phase="Failed"), gone, _pod(True, token="new")])
    api.delete_namespaced_pod = AsyncMock()
    api.create_namespaced_pod = AsyncMock()
    api.create_namespaced_secret = AsyncMock()
    k8s._api = api
    k8s._wait_for_ready = AsyncMock()
    sid = await k8s.provision(_spec())
    api.delete_namespaced_pod.assert_awaited_once()
    api.create_namespaced_pod.assert_awaited_once()
    api.create_namespaced_secret.assert_not_awaited()
    assert k8s._pods[sid].pod_ip == "10.0.0.7" and k8s.created(sid)


async def test_a_pod_another_worker_just_made_is_taken_over(k8s):
    api = MagicMock()
    api.read_namespaced_pod = AsyncMock(side_effect=[ApiException(status=404), _pod(True, token="theirs")])
    api.create_namespaced_pod = AsyncMock(side_effect=ApiException(status=409))
    k8s._api = api
    k8s._wait_for_ready = AsyncMock()
    sid = await k8s.provision(_spec())
    assert k8s._pods[sid].pod_ip == "10.0.0.7" and not k8s.created(sid)


async def test_its_destroy_deletes_the_pod_and_no_secret(k8s):
    api = MagicMock()
    api.read_namespaced_pod = AsyncMock(return_value=_pod(True))
    api.delete_namespaced_pod = AsyncMock()
    api.delete_namespaced_secret = AsyncMock()
    k8s._api = api
    sid = await k8s.provision(_spec())
    await k8s.destroy(sid)
    api.delete_namespaced_pod.assert_awaited_once()
    api.delete_namespaced_secret.assert_not_awaited()


def test_commands_get_the_images_environment_from_its_file(tmp_path):
    from surogates.tools.workspace_io import local

    f = tmp_path / "image-env.json"
    f.write_text(json.dumps({"PATH": "/usr/local/go/bin:/usr/bin", "GOPATH": "/go", "PROJECT_DIR": "/app"}))
    environ = {"PATH": "/usr/bin", "HOME": "/root", "PROJECT_DIR": "", "MCP_PROXY_TOKEN": "secret",
               "SUROGATES_IMAGE_SESSION": "1", "SUROGATES_IMAGE_ENV_FILE": str(f)}
    with patch.dict(os.environ, environ, clear=True):
        env = local._build_child_env()
    assert env == {"PATH": "/usr/local/go/bin:/usr/bin", "GOPATH": "/go", "PROJECT_DIR": "/app", "HOME": "/root"}


async def test_the_reaper_deletes_only_pods_at_rest_and_idle_by_their_executors_clock(k8s, monkeypatch):
    import httpx

    def pod(name, session, phase="Running", ip="10.0.0.9"):
        p = _pod(True, token="tok", ip=ip, phase=phase)
        p.metadata = SimpleNamespace(name=name, annotations={"surogates.ai/session": session})
        return p

    api = MagicMock()
    api.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=[
        pod("idle", "s-idle", ip="10.0.0.1"), pod("busy", "s-busy-tree", ip="10.0.0.2"),
        pod("used", "s-used", ip="10.0.0.3"), pod("ended", "s-ended", phase="Failed")]))
    api.delete_namespaced_pod = AsyncMock()
    k8s._api = api
    seen = {"10.0.0.1": {"idle_seconds": 900, "running": 0, "background_running": 0},
            "10.0.0.3": {"idle_seconds": 10, "running": 0, "background_running": 0}}
    real = httpx.AsyncClient

    def client(*a, **kw):
        def answer(request):
            assert request.url.path == "/retire"
            body = json.loads(request.content)
            got = dict(seen[request.url.host])
            got["retired"] = got["idle_seconds"] >= body["min_idle_seconds"]
            return httpx.Response(200, json=got)
        return real(transport=httpx.MockTransport(answer), timeout=5.0)
    monkeypatch.setattr(httpx, "AsyncClient", client)

    async def at_rest(session):
        return session != "s-busy-tree"
    assert await k8s.reap_images(at_rest, idle_seconds=300) == ["s-idle", "s-ended"]
    assert [c.args[0] for c in api.delete_namespaced_pod.await_args_list] == ["idle", "ended"]


def test_the_entry_clears_the_images_control_names_from_its_file_unless_the_pod_set_them(tmp_path):
    from surogates.image_entry import prepare

    f = tmp_path / "image-env.json"
    f.write_text(json.dumps({"PROJECT_DIR": "/app", "TERMINAL_TIMEOUT": "", "WORKSPACE_DIR": "/elsewhere", "PATH": "/x"}))
    environ = {"SUROGATES_IMAGE_ENV_FILE": str(f), "PROJECT_DIR": "/app", "TERMINAL_TIMEOUT": "",
               "WORKSPACE_DIR": "/app", "PATH": "/x"}
    prepare(environ)
    assert "PROJECT_DIR" not in environ and "TERMINAL_TIMEOUT" not in environ
    assert environ["WORKSPACE_DIR"] == "/app" and environ["PATH"] == "/x"


async def test_a_stop_deletes_the_sessions_pod_wherever_it_was_made(k8s):
    api = MagicMock()
    api.read_namespaced_pod = AsyncMock(return_value=_pod(True))
    api.delete_namespaced_pod = AsyncMock()
    k8s._api = api
    sid = await k8s.provision(_spec())
    await k8s.stop_image_of_session(SESSION)
    assert api.delete_namespaced_pod.await_args.args[0] == f"sbx-img-{SESSION}"
    assert sid not in k8s._pods
    # A worker that holds no entry deletes it all the same; one already gone is no error.
    api.delete_namespaced_pod = AsyncMock(side_effect=ApiException(status=404))
    await k8s.stop_image_of_session(SESSION)


async def test_the_pool_forgets_a_pod_that_is_not_ready_without_deleting_it(k8s):
    api = MagicMock()
    api.read_namespaced_pod = AsyncMock(return_value=_pod(True))
    api.delete_namespaced_pod = AsyncMock()
    k8s._api = api
    sid = await k8s.provision(_spec())
    k8s.forget(sid)
    assert sid not in k8s._pods and not k8s.created(sid)
    api.delete_namespaced_pod.assert_not_awaited()


def test_the_digest_setting_and_the_daemons_switches_hold_for_image_pods(k8s):
    k8s._token_env = "sha256"
    spec = SandboxSpec(session_id=SESSION, session_image=IMAGE, image_workdir="/app",
                       env={"ORG_ID": "o", "TOOL_EXECUTOR_ALLOW_LOCAL": "1", "FORWARDED_ALLOW_IPS": "*"})
    pod = k8s._image_pod_manifest("sbx1", image_pod_name(spec, "sbx1"), spec, executor_token="tok")
    env = _env(pod.spec.containers[0])
    from surogates.sandbox.executor_server import token_sha256

    assert "TOOL_EXECUTOR_TOKEN" not in env and env["TOOL_EXECUTOR_TOKEN_SHA256"] == token_sha256("tok")
    assert "TOOL_EXECUTOR_ALLOW_LOCAL" not in env and "FORWARDED_ALLOW_IPS" not in env and env["ORG_ID"] == "o"
