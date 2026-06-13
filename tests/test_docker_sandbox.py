"""Tests for surogates.sandbox.docker.DockerSandbox."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from aiohttp import web

from surogates.sandbox.base import SandboxSpec, SandboxStatus, SandboxUnavailableError
from surogates.sandbox.docker import DockerSandbox, _Entry


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._containers: dict[str, dict[str, Any]] = {}
        self.fail_next_run_with_port_conflict = False

    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        self.calls.append(args)
        if args[:2] == ["run", "-d"]:
            if self.fail_next_run_with_port_conflict:
                self.fail_next_run_with_port_conflict = False
                return (125, b"", b"Bind for 0.0.0.0:33000 failed: port is already allocated")
            cid = f"cid-{len(self._containers) + 1}"
            labels = {}
            for idx, arg in enumerate(args):
                if arg == "--label" and idx + 1 < len(args):
                    key, _, value = args[idx + 1].partition("=")
                    labels[key] = value
            self._containers[cid] = {"running": True, "labels": labels}
            return 0, cid.encode() + b"\n", b""
        if args[:2] == ["ps", "-aq"]:
            label = ""
            for idx, arg in enumerate(args):
                if arg == "--filter" and idx + 1 < len(args):
                    label = args[idx + 1].removeprefix("label=")
            key, _, value = label.partition("=")
            matches = [
                cid for cid, st in self._containers.items()
                if st.get("labels", {}).get(key) == value
            ]
            return 0, ("\n".join(matches) + ("\n" if matches else "")).encode(), b""
        if args[0] == "inspect":
            cid = args[-1]
            running = self._containers.get(cid, {}).get("running", False)
            return 0, (b"running" if running else b"exited") + b"\n", b""
        if args[0] in {"stop", "rm"}:
            cid = args[-1]
            if cid in self._containers and args[0] == "rm":
                del self._containers[cid]
            return 0, b"", b""
        return 0, b"", b""


@pytest.fixture()
def healthz_transport():
    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/healthz":
                return httpx.Response(200, text="ok")
            return httpx.Response(404)
    return T()


def _backend(docker, healthz_transport, **kw):
    return DockerSandbox(
        image="sbx-test:1",
        executor_port_base=33000,
        ready_timeout=5,
        network="bridge",
        docker=docker,
        httpx_transport=healthz_transport,
        **kw,
    )


async def _serve(handler) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_post("/execute", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, shutdown_timeout=0.5)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


class TestProvision:
    async def test_runs_docker_and_records_entry(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        sid = await backend.provision(SandboxSpec(session_id="root-1"))
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        joined = " ".join(run_call)
        assert "33000:8071" in joined
        assert "--network bridge" in joined
        assert "host.docker.internal:host-gateway" in joined
        assert "app=surogates-sandbox" in joined
        assert "surogates.session_id=root-1" in joined
        assert "TOOL_EXECUTOR_REQUIRE_FUSE=0" in joined
        assert any(a.startswith("TOOL_EXECUTOR_TOKEN=") for a in run_call)
        assert run_call[-1] == "sbx-test:1"
        assert backend._entries[sid].host_port == 33000
        await backend.aclose()

    async def test_reaps_stale_session_containers_before_provision(self, healthz_transport):
        docker = FakeDocker()
        # Pre-seed a stale container labelled for the same session.
        docker._containers["stale"] = {
            "running": True, "labels": {"surogates.session_id": "root-1"},
        }
        backend = _backend(docker, healthz_transport)
        await backend.provision(SandboxSpec(session_id="root-1"))
        # The stale container was listed and removed before the new run.
        assert any(c[:2] == ["ps", "-aq"] for c in docker.calls)
        assert "stale" not in docker._containers
        await backend.aclose()

    async def test_retries_next_port_on_conflict(self, healthz_transport):
        docker = FakeDocker()
        docker.fail_next_run_with_port_conflict = True
        backend = _backend(docker, healthz_transport)
        sid = await backend.provision(SandboxSpec(session_id="root-1"))
        run_calls = [c for c in docker.calls if c[:2] == ["run", "-d"]]
        assert len(run_calls) == 2
        assert "33000:8071" in " ".join(run_calls[0])
        assert "33001:8071" in " ".join(run_calls[1])
        assert backend._entries[sid].host_port == 33001
        await backend.aclose()

    async def test_run_failure_raises_unavailable(self, healthz_transport):
        class BrokenDocker(FakeDocker):
            async def run(self, args):
                self.calls.append(args)
                if args[:2] == ["run", "-d"]:
                    return 1, b"", b"Cannot connect to the Docker daemon"
                return 0, b"", b""
        backend = _backend(BrokenDocker(), healthz_transport)
        with pytest.raises(SandboxUnavailableError):
            await backend.provision(SandboxSpec(session_id="root-1"))
        await backend.aclose()


class TestWorkspaceMount:
    async def test_binds_workspace_when_path_valid(self, healthz_transport, tmp_path):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        await backend.provision(
            SandboxSpec(session_id="root-1", workspace_path=str(tmp_path))
        )
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        assert f"{tmp_path}:/workspace" in " ".join(run_call)
        await backend.aclose()

    async def test_no_mount_for_sentinel_or_missing(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        await backend.provision(
            SandboxSpec(session_id="root-1", workspace_path="/workspace")
        )
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        assert ":/workspace" not in " ".join(
            a for a in run_call if a != "/workspace"
        )
        # Explicit: no -v flag emitted.
        assert "-v" not in run_call
        await backend.aclose()


class TestExecute:
    async def test_passthrough(self, healthz_transport):
        async def handler(request):
            return web.Response(text='{"ok": true}', content_type="application/json")
        runner, port = await _serve(handler)
        backend = _backend(FakeDocker(), healthz_transport)
        backend._entries["sb"] = _Entry(
            sandbox_id="sb", container_id="cid-1", host_port=port,
            token="t", spec=SandboxSpec(timeout=5),
        )
        try:
            result = await backend.execute("sb", "terminal", '{"command": "ls"}')
            assert json.loads(result) == {"ok": True}
        finally:
            await runner.cleanup()
            await backend.aclose()

    async def test_401_marks_failed_and_raises(self, healthz_transport):
        async def handler(request):
            return web.Response(status=401, text="no")
        runner, port = await _serve(handler)
        backend = _backend(FakeDocker(), healthz_transport)
        entry = _Entry(
            sandbox_id="sb", container_id="cid-1", host_port=port,
            token="t", spec=SandboxSpec(timeout=5),
        )
        backend._entries["sb"] = entry
        try:
            with pytest.raises(SandboxUnavailableError):
                await backend.execute("sb", "terminal", "{}")
            assert entry.status == SandboxStatus.FAILED
        finally:
            await runner.cleanup()
            await backend.aclose()

    async def test_unknown_sandbox_raises_value_error(self, healthz_transport):
        backend = _backend(FakeDocker(), healthz_transport)
        with pytest.raises(ValueError):
            await backend.execute("nope", "terminal", "{}")
        await backend.aclose()


class TestStatusAndDestroy:
    async def test_status_running(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        sid = await backend.provision(SandboxSpec(session_id="root-1"))
        assert await backend.status(sid) == SandboxStatus.RUNNING
        await backend.aclose()

    async def test_status_unknown_is_terminated(self, healthz_transport):
        backend = _backend(FakeDocker(), healthz_transport)
        assert await backend.status("nope") == SandboxStatus.TERMINATED
        await backend.aclose()

    async def test_destroy_stops_and_removes(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        sid = await backend.provision(SandboxSpec(session_id="root-1"))
        await backend.destroy(sid)
        assert sid not in backend._entries
        assert any(c[0] == "stop" for c in docker.calls)
        assert any(c[0] == "rm" for c in docker.calls)
        await backend.aclose()

    async def test_destroy_for_session_filters_by_label(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        await backend.provision(SandboxSpec(session_id="root-1"))
        docker.calls.clear()
        await backend.destroy_for_session("root-1")
        assert any(c[:2] == ["ps", "-aq"] for c in docker.calls)
        assert any(c[0] == "rm" for c in docker.calls)
        await backend.aclose()


class TestHostServiceEnv:
    async def test_mcp_url_rewritten_and_token_injected(
        self, healthz_transport, monkeypatch
    ):
        monkeypatch.setattr(
            "surogates.tenant.auth.jwt.create_sandbox_token",
            lambda **kw: "mcp-tok",
        )
        docker = FakeDocker()
        backend = _backend(
            docker, healthz_transport,
            mcp_proxy_url="http://localhost:8001",
        )
        spec = SandboxSpec(
            session_id="11111111-1111-1111-1111-111111111111",
            env={
                "ORG_ID": "22222222-2222-2222-2222-222222222222",
                "USER_ID": "33333333-3333-3333-3333-333333333333",
                "SUROGATES_AGENT_ID": "agent-9",
            },
        )
        await backend.provision(spec)
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        joined = " ".join(run_call)
        assert "MCP_PROXY_URL=http://host.docker.internal:8001" in joined
        assert "MCP_PROXY_TOKEN=mcp-tok" in joined
        await backend.aclose()

    async def test_kb_env_passed_with_url_rewrite(
        self, healthz_transport, monkeypatch
    ):
        monkeypatch.setenv("SUROGATES_OPS_DB_URL", "postgresql://localhost:5432/ops")
        monkeypatch.setenv("SUROGATES_KB_HUB_ACCESS_KEY_ID", "ak-1")
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)
        await backend.provision(SandboxSpec(session_id="root-1"))
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        joined = " ".join(run_call)
        assert "SUROGATES_OPS_DB_URL=postgresql://host.docker.internal:5432/ops" in joined
        assert "SUROGATES_KB_HUB_ACCESS_KEY_ID=ak-1" in joined
        await backend.aclose()

    async def test_no_mcp_env_when_proxy_url_unset(self, healthz_transport):
        docker = FakeDocker()
        backend = _backend(docker, healthz_transport)  # mcp_proxy_url defaults to ""
        await backend.provision(SandboxSpec(session_id="root-1"))
        run_call = next(c for c in docker.calls if c[:2] == ["run", "-d"])
        assert "MCP_PROXY_URL=" not in " ".join(run_call)
        await backend.aclose()
