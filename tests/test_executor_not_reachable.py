"""What the Docker backends publish is out of the network's reach (SECURITY-1, 2026-11-20, P2).

The sandbox backend published its daemon's port, and the browser backend its REST, CDP and live-view ports, on
every interface: the host's network, and any container through the gateway, could reach them.  The daemon has a
token; CDP has none, so any sandbox could drive a session's browser.
"""

from __future__ import annotations

from tests.test_docker_sandbox import FakeDocker, _backend, healthz_transport  # noqa: F401
from surogates.sandbox.base import SandboxSpec


async def test_the_daemons_port_is_published_on_the_loopback_only(healthz_transport):
    docker = FakeDocker()
    backend = _backend(docker, healthz_transport)
    await backend.provision(SandboxSpec(session_id="root-1"))
    run = next(c for c in docker.calls if c[:2] == ["run", "-d"])
    assert run[run.index("-p") + 1] == "127.0.0.1:33000:8071"
    await backend.aclose()


async def test_the_browsers_ports_are_published_on_the_loopback_only():
    from surogates.browser.base import BrowserSpec
    from surogates.browser.process import ProcessBrowserBackend
    from tests.test_browser_process import FakeDocker as FakeBrowserDocker, fake_spec_json_transport  # noqa: F401
    import httpx

    class Ready(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, json={"ready": True})

    docker = FakeBrowserDocker()
    backend = ProcessBrowserBackend(
        image="kernel-test:1", rest_port_base=30000, cdp_port_base=31000, live_view_port_base=32000,
        docker=docker, httpx_transport=Ready(),
    )
    await backend.provision(BrowserSpec(image="kernel-test:1"))
    run = docker.calls[0]
    published = [run[i + 1] for i, arg in enumerate(run) if arg == "-p"]
    assert published == ["127.0.0.1:30000:10001", "127.0.0.1:31000:9222", "127.0.0.1:32000:8080"]

