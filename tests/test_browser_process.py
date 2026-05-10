"""Tests for surogates.browser.process.ProcessBrowserBackend."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from surogates.browser.base import BrowserSpec, BrowserStatus
from surogates.browser.process import ProcessBrowserBackend


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._containers: dict[str, dict[str, Any]] = {}

    async def run(self, args: list[str]) -> tuple[int, bytes, bytes]:
        self.calls.append(args)
        if args[:2] == ["run", "-d"]:
            cid = f"cid-{len(self._containers) + 1}"
            self._containers[cid] = {"running": True}
            return 0, cid.encode() + b"\n", b""
        if args[0] == "inspect":
            cid = args[-1]
            running = self._containers.get(cid, {}).get("running", False)
            return 0, (b"running" if running else b"exited") + b"\n", b""
        if args[0] in {"stop", "rm"}:
            cid = args[-1]
            if cid in self._containers:
                self._containers[cid]["running"] = False
                if args[0] == "rm":
                    del self._containers[cid]
            return 0, b"", b""
        return 0, b"", b""


@pytest.fixture()
def fake_spec_json_transport():
    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/spec.json":
                return httpx.Response(200, json={"ready": True})
            return httpx.Response(404)

    return T()


class TestProvision:
    async def test_provision_runs_docker_and_returns_endpoint(
        self, fake_spec_json_transport
    ) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="kernel-test:1",
            rest_port_base=30000,
            cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker,
            httpx_transport=fake_spec_json_transport,
        )
        bid, endpoint = await backend.provision(BrowserSpec(image="kernel-test:1"))
        assert bid == "cid-1"
        assert endpoint.rest_url == "http://127.0.0.1:30000"
        assert endpoint.cdp_url == "ws://127.0.0.1:31000"
        assert endpoint.live_view_url == "ws://127.0.0.1:32000"
        run_call = docker.calls[0]
        assert run_call[0] == "run"
        assert "-d" in run_call
        joined = " ".join(run_call)
        assert "30000:10001" in joined
        assert "31000:9222" in joined
        assert "32000:6080" in joined
        assert run_call[-1] == "kernel-test:1"

    async def test_provision_increments_port_for_second_browser(
        self, fake_spec_json_transport
    ) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i",
            rest_port_base=30000,
            cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker,
            httpx_transport=fake_spec_json_transport,
        )
        _b1, ep1 = await backend.provision(BrowserSpec())
        _b2, ep2 = await backend.provision(BrowserSpec())
        assert ep1.rest_url.endswith(":30000")
        assert ep2.rest_url.endswith(":30001")

    async def test_provision_mounts_workspace_when_configured(
        self,
        fake_spec_json_transport,
        tmp_path,
    ) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i",
            rest_port_base=30000,
            cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker,
            httpx_transport=fake_spec_json_transport,
        )
        await backend.provision(BrowserSpec(workspace_path=str(tmp_path)))

        run_call = docker.calls[0]
        joined = " ".join(run_call)
        assert f"{tmp_path}:/workspace" in joined
        assert "WORKSPACE_DIR=/workspace" in joined
        assert "HOME=/workspace" in joined

    async def test_provision_cleans_up_container_when_readiness_times_out(self) -> None:
        class NeverReadyTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(503, json={"ready": False})

        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i",
            rest_port_base=30000,
            cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker,
            httpx_transport=NeverReadyTransport(),
        )
        with pytest.raises(Exception):
            await backend.provision(BrowserSpec(pod_ready_timeout=0))

        verbs = [call[0] for call in docker.calls]
        assert "stop" in verbs
        assert "rm" in verbs


class TestStatus:
    async def test_status_running(self, fake_spec_json_transport) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i",
            rest_port_base=30000,
            cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker,
            httpx_transport=fake_spec_json_transport,
        )
        bid, _ = await backend.provision(BrowserSpec())
        assert await backend.status(bid) == BrowserStatus.RUNNING

    async def test_status_terminated_after_destroy(self, fake_spec_json_transport) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i",
            rest_port_base=30000,
            cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker,
            httpx_transport=fake_spec_json_transport,
        )
        bid, _ = await backend.provision(BrowserSpec())
        await backend.destroy(bid)
        assert await backend.status(bid) == BrowserStatus.TERMINATED


class TestDestroy:
    async def test_destroy_runs_stop_and_rm(self, fake_spec_json_transport) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i",
            rest_port_base=30000,
            cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker,
            httpx_transport=fake_spec_json_transport,
        )
        bid, _ = await backend.provision(BrowserSpec())
        await backend.destroy(bid)
        verbs = [call[0] for call in docker.calls]
        assert "stop" in verbs
        assert "rm" in verbs

    async def test_destroy_unknown_is_noop(self, fake_spec_json_transport) -> None:
        docker = FakeDocker()
        backend = ProcessBrowserBackend(
            image="i",
            rest_port_base=30000,
            cdp_port_base=31000,
            live_view_port_base=32000,
            docker=docker,
            httpx_transport=fake_spec_json_transport,
        )
        await backend.destroy("never-provisioned")
