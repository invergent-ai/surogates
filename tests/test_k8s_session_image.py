"""Kubernetes: a session's own image (session config ``sandbox_image``) runs in one pod per session.

The pod is the task's image with the executor bundle copied in by an init container; a second init container writes
the image's own environment for the commands; the sandbox container runs the bundle's executor as the image's user, in
the session's folder, with no storage sidecar, service account token or service links, the token's digest only and
seccomp RuntimeDefault.  A worker that finds the session's pod ready takes it over (its token checked); one starting is
waited for, never deleted; one that ended is replaced; every delete names the pod's uid.  A Stop ends its commands and
keeps its files.  The reaper takes this deployment's pods at rest.
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
NAME = f"sbx-img-{SESSION}"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setenv("SUROGATES_JWT_SECRET", "test-signing-secret")


@pytest.fixture()
def k8s() -> K8sSandbox:
    backend = K8sSandbox(namespace="ns", service_account="sa", pod_ready_timeout=5, executor_port=8071,
                         storage_settings=MagicMock(endpoint="http://minio:9000", access_key="k", secret_key="s",
                                                    region=""),
                         s3fs_image="s3fs:test", executor_bundle_image=BUNDLE, image_deployment="stack-a")
    backend._executor_answers = AsyncMock(return_value=200)
    return backend


def _spec(**kw) -> SandboxSpec:
    return SandboxSpec(session_id=SESSION, session_image=IMAGE, image_workdir="/app", env={"ORG_ID": "o"}, **kw)


def _env(container) -> dict[str, str]:
    return {e.name: e.value for e in container.env or []}


def _pod(ready: bool = True, *, uid: str = "uid-1", ip: str = "10.0.0.7", phase: str = "Running",
         deleting: bool = False, waiting: str = "", terminated: int | None = None, name: str = NAME,
         session: str = SESSION):
    sandbox_state = SimpleNamespace(
        waiting=SimpleNamespace(reason=waiting, message="") if waiting else None,
        terminated=(SimpleNamespace(exit_code=terminated, reason="Error", message="")
                    if terminated is not None else None),
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, uid=uid, deletion_timestamp="now" if deleting else None,
                                 annotations={"surogates.ai/session": session}),
        spec=SimpleNamespace(containers=[]),
        status=SimpleNamespace(pod_ip=ip, phase=phase,
                               conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")],
                               container_statuses=[SimpleNamespace(name="sandbox", state=sandbox_state)],
                               init_container_statuses=[]),
    )


def _api(*reads, **kw):
    api = MagicMock()
    api.read_namespaced_pod = AsyncMock(side_effect=list(reads))
    api.create_namespaced_pod = AsyncMock(**kw.get("create", {}))
    api.delete_namespaced_pod = AsyncMock()
    api.create_namespaced_secret = AsyncMock()
    api.delete_namespaced_secret = AsyncMock()
    return api


GONE = ApiException(status=404)


def test_the_pod_runs_the_bundles_executor_in_the_image(k8s):
    from surogates.sandbox.executor_server import token_sha256

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
    # The token's digest only: the session's token outlives the pod, and the pod's spec is readable.
    assert "TOOL_EXECUTOR_TOKEN" not in env and env["TOOL_EXECUTOR_TOKEN_SHA256"] == token_sha256("tok")
    assert env["WORKSPACE_DIR"] == "/app" and env["TOOL_EXECUTOR_PORT"] == "8071"
    assert env["TOOL_EXECUTOR_REQUIRE_FUSE"] == "0" and env["SUROGATES_IMAGE_SESSION"] == "1"
    assert env["SUROGATES_IMAGE_ENV_FILE"] == IMAGE_ENV_FILE and env["ORG_ID"] == "o"
    assert env["SUROGATES_IMAGE_ENV_UNSET"] == ""
    assert set(env["SUROGATES_IMAGE_ENV_SET"].split(",")) == set(env) - {"SUROGATES_IMAGE_ENV_SET"}
    assert "PROJECT_DIR" not in env
    mounts = {m.mount_path: m for m in sandbox.volume_mounts}
    assert mounts["/opt/surogates"].read_only and mounts["/surogates-state"].read_only
    assert sandbox.security_context.allow_privilege_escalation is False
    assert sandbox.security_context.capabilities.drop == ["NET_RAW", "MKNOD", "SETFCAP"]
    assert sandbox.security_context.run_as_user is None  # the image's own user
    assert spec.security_context.seccomp_profile.type == "RuntimeDefault"
    # No storage sidecar, no API token, no service links, no deadline (the reaper ends it at rest).
    assert [c.name for c in spec.containers] == ["sandbox"]
    assert spec.automount_service_account_token is False and spec.enable_service_links is False
    assert spec.active_deadline_seconds is None
    assert pod.metadata.labels["surogates.ai/session-image"] == "true"
    assert pod.metadata.labels["surogates.ai/deployment"] == "stack-a"
    assert pod.metadata.name == f"sbx-img-{SESSION}" and len(pod.metadata.name) <= 63


def test_without_its_folder_the_bundle_or_the_deployment_mark_it_is_refused_and_ssh_never(k8s):
    with pytest.raises(SandboxUnavailableError, match="sandbox_workdir"):
        k8s._image_pod_manifest("s", "p", SandboxSpec(session_id=SESSION, session_image=IMAGE), executor_token="t")
    with pytest.raises(SandboxUnavailableError, match="SSH"):
        k8s._image_pod_manifest("s", "p", _spec(ssh_key_material="key"), executor_token="t")
    k8s._image_deployment = ""
    with pytest.raises(SandboxUnavailableError, match="image_deployment"):
        k8s._image_pod_manifest("s", "p", _spec(), executor_token="t")
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


async def test_a_ready_pod_of_the_session_is_taken_over_once_its_token_answers(k8s):
    k8s._api = _api(_pod())
    sid = await k8s.provision(_spec())
    entry = k8s._pods[sid]
    from surogates.sandbox.docker_image import session_token

    assert (entry.pod_ip, entry.token, entry.status, entry.uid) == ("10.0.0.7", session_token(SESSION),
                                                                   SandboxStatus.RUNNING, "uid-1")
    k8s._api.create_namespaced_pod.assert_not_awaited()
    assert not k8s.created(sid)


async def test_a_pod_of_another_signing_secret_is_replaced(k8s):
    k8s._executor_answers = AsyncMock(side_effect=[401, 200])
    k8s._api = _api(_pod(uid="old"), GONE, _pod(uid="new"), create={"return_value": _pod(uid="new")})
    with patch("surogates.sandbox.kubernetes_image.asyncio.sleep", AsyncMock()):
        sid = await k8s.provision(_spec())
    delete = k8s._api.delete_namespaced_pod.await_args
    assert delete.args[0] == NAME and delete.kwargs["body"].preconditions.uid == "old"
    assert k8s._pods[sid].uid == "new" and k8s.created(sid)


async def test_an_ended_pod_is_replaced_by_its_uid_and_no_storage_secret_is_made(k8s):
    k8s._api = _api(_pod(False, uid="old", phase="Failed"), GONE, _pod(uid="new"),
                    create={"return_value": _pod(uid="new")})
    with patch("surogates.sandbox.kubernetes_image.asyncio.sleep", AsyncMock()):
        sid = await k8s.provision(_spec())
    assert k8s._api.delete_namespaced_pod.await_args.kwargs["body"].preconditions.uid == "old"
    k8s._api.create_namespaced_pod.assert_awaited_once()
    k8s._api.create_namespaced_secret.assert_not_awaited()
    assert k8s._pods[sid].pod_ip == "10.0.0.7" and k8s.created(sid)


async def test_a_pod_starting_is_waited_for_never_deleted(k8s):
    """Pending (another worker's create, an image pulling), or running but not ready yet: waited for."""
    k8s._api = _api(_pod(False, phase="Pending"), _pod(False), _pod())
    with patch("surogates.sandbox.kubernetes_image.asyncio.sleep", AsyncMock()):
        sid = await k8s.provision(_spec())
    k8s._api.delete_namespaced_pod.assert_not_awaited()
    k8s._api.create_namespaced_pod.assert_not_awaited()
    assert k8s._pods[sid].uid == "uid-1"


async def test_a_pod_that_never_gets_ready_is_kept_and_the_call_fails(k8s, monkeypatch):
    from surogates.sandbox import kubernetes_image

    monkeypatch.setattr(kubernetes_image, "_TAKE_OVER_S", 0.0)
    k8s._pod_ready_timeout = 0
    k8s._api = _api(*[_pod(False)] * 3)
    with pytest.raises(SandboxUnavailableError, match="not ready"):
        await k8s.provision(_spec())
    k8s._api.delete_namespaced_pod.assert_not_awaited()


async def test_a_pod_this_provision_made_that_cannot_pull_is_deleted_and_says_why(k8s):
    k8s._api = _api(GONE, _pod(False, uid="mine", phase="Pending", waiting="ImagePullBackOff"),
                    create={"return_value": _pod(uid="mine")})
    with pytest.raises(SandboxUnavailableError, match="ImagePullBackOff"):
        await k8s.provision(_spec())
    assert k8s._api.delete_namespaced_pod.await_args.kwargs["body"].preconditions.uid == "mine"


async def test_a_pod_another_worker_just_made_is_taken_over(k8s):
    k8s._api = _api(GONE, _pod(False, phase="Pending"), _pod(uid="theirs"),
                    create={"side_effect": ApiException(status=409)})
    with patch("surogates.sandbox.kubernetes_image.asyncio.sleep", AsyncMock()):
        sid = await k8s.provision(_spec())
    assert k8s._pods[sid].uid == "theirs" and not k8s.created(sid)
    k8s._api.delete_namespaced_pod.assert_not_awaited()


async def test_a_pod_being_deleted_is_waited_out_then_made_again(k8s):
    k8s._api = _api(_pod(uid="going", deleting=True), GONE, _pod(uid="new"), create={"return_value": _pod(uid="new")})
    with patch("surogates.sandbox.kubernetes_image.asyncio.sleep", AsyncMock()):
        sid = await k8s.provision(_spec())
    assert k8s._pods[sid].uid == "new"


async def test_an_entry_whose_pod_was_replaced_reads_as_gone(k8s):
    k8s._api = _api(_pod(uid="first"))
    sid = await k8s.provision(_spec())
    k8s._api.read_namespaced_pod = AsyncMock(return_value=_pod(uid="second"))
    assert await k8s.gone(sid)
    assert await k8s.status(sid) == SandboxStatus.TERMINATED and sid not in k8s._pods


async def test_a_stop_ends_the_commands_and_keeps_the_pod(k8s, monkeypatch):
    import httpx

    asked = []
    real = httpx.AsyncClient

    def client(*a, **kw):
        def answer(request):
            asked.append((request.url.host, request.url.path, request.headers["authorization"]))
            return httpx.Response(200, json={"killed": 2})
        return real(transport=httpx.MockTransport(answer), timeout=5.0)
    k8s._api = _api(_pod(), _pod())
    sid = await k8s.provision(_spec())
    monkeypatch.setattr(httpx, "AsyncClient", client)
    await k8s.stop_image_of_session(SESSION)
    from surogates.sandbox.docker_image import session_token

    assert asked == [("10.0.0.7", "/stop", f"Bearer {session_token(SESSION)}")]
    k8s._api.delete_namespaced_pod.assert_not_awaited()
    assert sid in k8s._pods


async def test_a_stop_deletes_a_pod_whose_executor_does_not_answer(k8s, monkeypatch):
    import httpx

    real = httpx.AsyncClient

    def client(*a, **kw):
        def answer(request):
            raise httpx.ConnectError("refused")
        return real(transport=httpx.MockTransport(answer), timeout=5.0)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    k8s._api = _api(_pod(uid="stuck"))
    await k8s.stop_image_of_session(SESSION)
    assert k8s._api.delete_namespaced_pod.await_args.kwargs["body"].preconditions.uid == "stuck"


async def test_destroy_of_an_image_sandbox_stops_it_and_keeps_its_pod(k8s):
    k8s._api = _api(_pod(), _pod())
    sid = await k8s.provision(_spec())
    k8s._stop_image_pod = AsyncMock()
    await k8s.destroy(sid)
    k8s._stop_image_pod.assert_awaited_once()
    k8s._api.delete_namespaced_pod.assert_not_awaited()
    k8s._api.delete_namespaced_secret.assert_not_awaited()
    assert sid not in k8s._pods


async def test_the_pool_forgets_a_pod_that_is_not_ready_without_deleting_it(k8s):
    k8s._api = _api(_pod())
    sid = await k8s.provision(_spec())
    k8s.forget(sid)
    assert sid not in k8s._pods and not k8s.created(sid)
    k8s._api.delete_namespaced_pod.assert_not_awaited()


def test_commands_get_the_images_environment_from_its_file(tmp_path):
    from surogates.tools.workspace_io import local

    f = tmp_path / "image-env.json"
    f.write_text(json.dumps({"PATH": "/usr/local/go/bin:/usr/bin", "GOPATH": "/go", "PROJECT_DIR": "/app"}))
    environ = {"PATH": "/usr/bin", "HOME": "/root", "PROJECT_DIR": "", "MCP_PROXY_TOKEN": "secret",
               "SUROGATES_IMAGE_SESSION": "1", "SUROGATES_IMAGE_ENV_FILE": str(f)}
    with patch.dict(os.environ, environ, clear=True):
        env = local._build_child_env()
    assert env == {"PATH": "/usr/local/go/bin:/usr/bin", "GOPATH": "/go", "PROJECT_DIR": "/app", "HOME": "/root"}


async def test_the_reaper_takes_only_this_deployments_pods_at_rest_and_retired_or_ended(k8s, monkeypatch):
    import httpx

    api = MagicMock()
    api.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=[
        _pod(name="idle", session="s-idle", ip="10.0.0.1", uid="u-idle"),
        _pod(name="busy", session="s-busy-tree", ip="10.0.0.2"),
        _pod(name="used", session="s-used", ip="10.0.0.3"),
        _pod(False, name="ended", session="s-ended", phase="Failed", uid="u-ended"),
        _pod(name="going", session="s-going", deleting=True)]))
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
    assert await k8s.reap_images(at_rest, idle_seconds=300) == [("s-idle", "idle"), ("s-ended", "ended")]
    deletes = api.delete_namespaced_pod.await_args_list
    assert [(c.args[0], c.kwargs["body"].preconditions.uid) for c in deletes] == [("idle", "u-idle"),
                                                                                    ("ended", "u-ended")]
    selector = api.list_namespaced_pod.await_args.kwargs["label_selector"]
    assert "surogates.ai/session-image=true" in selector and "surogates.ai/deployment=stack-a" in selector


async def test_no_bundle_or_no_mark_no_reaping(k8s):
    k8s._api = MagicMock()
    k8s._image_deployment = ""

    async def at_rest(session):
        return True
    assert await k8s.reap_images(at_rest, idle_seconds=300) == []
    k8s._api.list_namespaced_pod.assert_not_called()


def test_the_entry_clears_the_images_control_names_but_those_the_pod_set(tmp_path):
    from surogates.image_entry import prepare

    f = tmp_path / "image-env.json"
    f.write_text(json.dumps({"PROJECT_DIR": "/app", "TERMINAL_TIMEOUT": "", "WORKSPACE_DIR": "/app",
                             "TOOL_EXECUTOR_REQUIRE_FUSE": "0", "PATH": "/x"}))
    environ = {"SUROGATES_IMAGE_ENV_FILE": str(f), "PROJECT_DIR": "/app", "TERMINAL_TIMEOUT": "",
               "WORKSPACE_DIR": "/app", "TOOL_EXECUTOR_REQUIRE_FUSE": "0", "PATH": "/x",
               "SUROGATES_IMAGE_ENV_SET": "WORKSPACE_DIR,TOOL_EXECUTOR_REQUIRE_FUSE,SUROGATES_IMAGE_ENV_FILE"}
    prepare(environ)
    assert "PROJECT_DIR" not in environ and "TERMINAL_TIMEOUT" not in environ
    # The pod's own values stay, even when the image declared the same.
    assert environ["WORKSPACE_DIR"] == "/app" and environ["TOOL_EXECUTOR_REQUIRE_FUSE"] == "0"
    assert environ["PATH"] == "/x"


def test_the_daemons_switches_never_come_from_the_spec(k8s):
    spec = SandboxSpec(session_id=SESSION, session_image=IMAGE, image_workdir="/app",
                       env={"ORG_ID": "o", "TOOL_EXECUTOR_ALLOW_LOCAL": "1", "FORWARDED_ALLOW_IPS": "*"})
    pod = k8s._image_pod_manifest("sbx1", image_pod_name(spec, "sbx1"), spec, executor_token="tok")
    env = _env(pod.spec.containers[0])
    assert "TOOL_EXECUTOR_ALLOW_LOCAL" not in env and "FORWARDED_ALLOW_IPS" not in env and env["ORG_ID"] == "o"


async def test_the_executors_stop_ends_every_command_from_outside_only(tmp_path):
    import httpx

    from surogates.sandbox import executor_server

    app = executor_server.create_app(token="tok", workspace=str(tmp_path), require_fuse=False, allow_local=False)
    auth = {"Authorization": "Bearer tok"}

    async def post(peer):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 40000)),
                                     base_url="http://198.51.100.2:8071") as http:
            return await http.post("/stop", headers=auth)
    # Never a real kill(-1) here: the ending is a mock, and os.kill too.
    with patch.object(executor_server.os, "kill") as kill, \
            patch.object(executor_server, "_end_every_process") as end, \
            patch("surogates.tools.utils.process_registry.process_registry.kill_all", return_value=3):
        assert (await post("127.0.0.1")).status_code == 403
        end.assert_not_called()
        with patch.dict(os.environ, {"SUROGATES_IMAGE_SESSION": ""}):
            r = await post("198.51.100.1")
        assert r.status_code == 200 and r.json() == {"killed": 3}
        end.assert_not_called()  # outside a session's own image: the registry's commands only
        with patch.dict(os.environ, {"SUROGATES_IMAGE_SESSION": "1"}):
            assert (await post("198.51.100.1")).status_code == 200
        end.assert_called_once_with()
        kill.assert_not_called()
