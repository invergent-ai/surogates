"""A sandbox's environment holds its daemon token's SHA-256, not the token (SECURITY-1 P4, part B).

Commands run as the uid of tini (PID 1) and of the daemon: whatever the container's
environment holds, ``/proc/1/environ`` shows them.  With
``sandbox.executor_token_env=sha256`` it holds ``TOOL_EXECUTOR_TOKEN_SHA256``; the daemon
compares a caller's bearer by its digest, and the 256-bit token itself is only the
worker's.  No first-call window, nothing to race (an earlier design that set the token
by the worker's first call lost that race to anything else on the network).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from surogates.sandbox import executor_server
from surogates.sandbox.base import SandboxSpec, SandboxUnavailableError
from surogates.sandbox.executor_server import TOKEN_SHA256_ENV, _token_ok, token_sha256
from surogates.tools.builtin import terminal
from surogates.tools.registry import ToolRegistry
from tests.test_docker_sandbox import FakeDocker, _backend, healthz_transport  # noqa: F401

TOKEN = "t" * 43


def test_a_bearer_is_compared_by_its_digest():
    held = f"sha256:{token_sha256(TOKEN)}"
    assert _token_ok(f"Bearer {TOKEN}", held)
    assert not _token_ok("Bearer " + "x" * 43, held)
    assert not _token_ok(f"Bearer {token_sha256(TOKEN)}", held)  # the digest itself is no bearer
    assert not _token_ok("Bearer é" * 3, held)  # a non-ASCII bearer is refused, not an error
    assert _token_ok(f"Bearer {TOKEN}", TOKEN) and not _token_ok("Bearer é", TOKEN)  # the plain token, as before


@pytest.mark.parametrize("plain", [False, True])
async def test_the_daemon_takes_the_digest_or_the_token_from_its_environment(monkeypatch, tmp_path, plain):
    import uvicorn

    served = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.update(app=app))
    monkeypatch.setattr(executor_server, "init_registry", lambda: None)
    monkeypatch.setattr(executor_server, "set_dumpable", lambda on: True)
    registry = ToolRegistry()
    terminal.register(registry)
    monkeypatch.setattr(executor_server, "_REGISTRY", registry)
    monkeypatch.delenv("TOOL_EXECUTOR_TOKEN", raising=False)
    monkeypatch.delenv(TOKEN_SHA256_ENV, raising=False)
    monkeypatch.setenv("TOOL_EXECUTOR_TOKEN" if plain else TOKEN_SHA256_ENV, TOKEN if plain else token_sha256(TOKEN))
    monkeypatch.setenv("TOOL_EXECUTOR_REQUIRE_FUSE", "0")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    executor_server.main()
    transport = httpx.ASGITransport(app=served["app"], client=("198.51.100.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://198.51.100.2:8071") as http:
        body = {"name": "terminal", "args": {"command": "echo hi"}}
        ok = await http.post("/execute", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        bad = await http.post("/execute", json=body, headers={"Authorization": "Bearer " + "x" * 43})
    assert ok.status_code == 200 and bad.status_code == 401


@pytest.mark.parametrize("digest", [None, "not-a-digest", "\u00e9" * 64])
def test_without_a_usable_value_the_daemon_does_not_start(monkeypatch, digest):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("served without a token"))
    monkeypatch.setattr(executor_server, "init_registry", lambda: None)
    monkeypatch.delenv("TOOL_EXECUTOR_TOKEN", raising=False)
    monkeypatch.delenv(TOKEN_SHA256_ENV, raising=False)
    if digest is not None:
        monkeypatch.setenv(TOKEN_SHA256_ENV, digest)
    with pytest.raises(SystemExit):
        executor_server.main()


async def test_with_both_the_digest_wins(monkeypatch, tmp_path):
    import uvicorn

    served = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.update(app=app))
    monkeypatch.setattr(executor_server, "init_registry", lambda: None)
    monkeypatch.setattr(executor_server, "set_dumpable", lambda on: True)
    monkeypatch.setenv("TOOL_EXECUTOR_TOKEN", "y" * 43)
    monkeypatch.setenv(TOKEN_SHA256_ENV, token_sha256(TOKEN))
    monkeypatch.setenv("TOOL_EXECUTOR_REQUIRE_FUSE", "0")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    executor_server.main()
    transport = httpx.ASGITransport(app=served["app"], client=("198.51.100.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://198.51.100.2:8071") as http:
        stale = await http.post("/execute", json={"name": "x"}, headers={"Authorization": "Bearer " + "y" * 43})
    assert stale.status_code == 401


async def test_docker_puts_only_the_digest_in_the_container(healthz_transport):
    docker = FakeDocker()
    backend = _backend(docker, healthz_transport, token_env="sha256")
    sid = await backend.provision(SandboxSpec(session_id="root-1", env={"TOOL_EXECUTOR_TOKEN_SHA256": "spoof"}))
    run = next(c for c in docker.calls if c[:2] == ["run", "-d"])
    token = backend._entries[sid].token
    assert f"{TOKEN_SHA256_ENV}={token_sha256(token)}" in run
    assert not any(a.startswith("TOOL_EXECUTOR_TOKEN=") for a in run) and not any(token in a for a in run)
    assert f"{TOKEN_SHA256_ENV}=spoof" not in run  # never from a spec
    await backend.aclose()


async def test_docker_in_plain_mode_is_as_before(healthz_transport):
    docker = FakeDocker()
    backend = _backend(docker, healthz_transport)
    sid = await backend.provision(SandboxSpec(session_id="root-1"))
    run = next(c for c in docker.calls if c[:2] == ["run", "-d"])
    assert f"TOOL_EXECUTOR_TOKEN={backend._entries[sid].token}" in run
    await backend.aclose()


async def test_an_image_from_before_the_digest_is_named_as_the_cause():
    class Exits(FakeDocker):
        async def run(self, args):
            if args[0] == "inspect" and "{{.State.Running}}" in args:
                self.calls.append(args)
                return 1, b"", b"No such container"
            return await super().run(args)

    class NeverReady(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("refused")

    backend = _backend(Exits(), NeverReady(), token_env="sha256")
    backend._ready_timeout = 0.2
    with pytest.raises(SandboxUnavailableError, match="SUROGATES_SANDBOX_EXECUTOR_TOKEN_ENV=plain"):
        await backend.provision(SandboxSpec(session_id="root-1"))
    await backend.aclose()


async def test_kubernetes_puts_only_the_digest_in_the_pod():
    from surogates.sandbox.kubernetes import K8sSandbox

    sandbox = K8sSandbox(namespace="ns", storage_settings=MagicMock(endpoint="http://m:9000", access_key="k",
                                                                   secret_key="s", region=""), token_env="sha256")
    api = MagicMock()
    api.create_namespaced_pod = AsyncMock()
    pod = MagicMock()
    pod.status.pod_ip = "10.42.0.7"
    api.read_namespaced_pod = AsyncMock(return_value=pod)
    with patch.object(sandbox, "_get_api", AsyncMock(return_value=api)), \
         patch.object(sandbox, "_create_s3_secret", AsyncMock()), \
         patch.object(sandbox, "_wait_for_ready", AsyncMock()):
        sid = await sandbox.provision(SandboxSpec(env={"TOOL_EXECUTOR_TOKEN": "spoof"}))
    main = api.create_namespaced_pod.await_args.args[1].spec.containers[0]
    env = {e.name: e.value for e in main.env}
    assert env[TOKEN_SHA256_ENV] == token_sha256(sandbox._pods[sid].token)
    assert "TOOL_EXECUTOR_TOKEN" not in env


async def test_kubernetes_names_a_possible_old_image_and_removes_the_pod():
    from surogates.sandbox.kubernetes import K8sSandbox

    sandbox = K8sSandbox(namespace="ns", storage_settings=MagicMock(endpoint="http://m:9000", access_key="k",
                                                                   secret_key="s", region=""), token_env="sha256")
    api = MagicMock()
    api.create_namespaced_pod = AsyncMock()
    with patch.object(sandbox, "_get_api", AsyncMock(return_value=api)), \
         patch.object(sandbox, "_create_s3_secret", AsyncMock()), \
         patch.object(sandbox, "_wait_for_ready", AsyncMock(side_effect=TimeoutError("not ready"))), \
         patch.object(sandbox, "_destroy_entry", AsyncMock()) as destroyed:
        with pytest.raises(SandboxUnavailableError, match="if the image predates TOOL_EXECUTOR_TOKEN_SHA256"):
            await sandbox.provision(SandboxSpec())
    destroyed.assert_awaited_once()
    assert sandbox._pods == {}


def test_the_setting_defaults_to_plain_until_the_image_has_the_digest():
    from surogates.config import SandboxSettings

    assert SandboxSettings().executor_token_env == "plain"
