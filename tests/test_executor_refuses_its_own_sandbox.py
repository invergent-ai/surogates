"""The sandbox daemon refuses ``/execute`` from inside its own sandbox (SECURITY-1 P1, 2026-11-20).

A model read the daemon's token from ``/proc/7/environ`` and called ``/execute``
with it: tools ran past the harness (no event, its own timeout, internal
commands such as ``_background`` and ``_checkpoint``).  A command can only
connect from the sandbox's own addresses; the worker's calls come from outside.
"""

from __future__ import annotations

import json

import httpx
import pytest

from surogates.sandbox import executor_server
from surogates.sandbox.executor_server import from_inside
from surogates.tools.builtin import terminal
from surogates.tools.registry import ToolRegistry

AUTH = {"Authorization": "Bearer secret-token"}


@pytest.fixture()
def app_for(monkeypatch, tmp_path):
    registry = ToolRegistry()
    terminal.register(registry)
    monkeypatch.setattr(executor_server, "_REGISTRY", registry)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text(f"geesefs {workspace} fuse.geesefs rw 0 0\n")

    def make(**kw):
        return executor_server.create_app(token="secret-token", workspace=str(workspace), mounts_path=str(mounts), **kw)
    return make


async def _post(app, *, client: tuple[str, int], server: str = "http://198.51.100.2:8071"):
    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=transport, base_url=server) as http:
        return await http.post("/execute", headers=AUTH, json={"name": "terminal", "args": {"command": "echo hi"}})


@pytest.mark.parametrize(
    ("peer", "why"),
    [(("127.0.0.1", 40000), "loopback"), (("::1", 40000), "loopback v6"), (("198.51.100.2", 40000), "its own address")],
)
async def test_a_call_from_inside_the_sandbox_is_refused_even_with_the_token(app_for, peer, why):
    resp = await _post(app_for(allow_local=False), client=peer)
    assert resp.status_code == 403, why
    assert "inside the sandbox" in resp.text


# Documentation addresses (RFC 5737): no host running the tests has them as its own.
@pytest.mark.parametrize("peer", [("198.51.100.1", 50000), ("203.0.113.7", 50000)], ids=["docker gateway", "worker pod"])
async def test_the_workers_call_from_outside_runs(app_for, peer):
    resp = await _post(app_for(allow_local=False), client=peer)
    assert resp.status_code == 200
    assert json.loads(resp.text)["output"] == "hi"


async def test_healthz_stays_open_to_the_probes(app_for):
    transport = httpx.ASGITransport(app=app_for(allow_local=False), client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://198.51.100.2:8071") as http:
        assert (await http.get("/healthz")).status_code == 200


async def test_with_the_switch_the_in_pod_debug_client_works(app_for):
    resp = await _post(app_for(allow_local=True), client=("127.0.0.1", 40000))
    assert resp.status_code == 200


@pytest.mark.parametrize(("switch", "status"), [(None, 403), ("1", 200)])
async def test_the_daemon_as_main_starts_it_refuses_local_calls_unless_switched(monkeypatch, tmp_path, switch, status):
    """main() as the image runs it: uvicorn's real arguments, and the app it serves."""
    import uvicorn

    served = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.update(app=app, **kwargs))
    monkeypatch.setattr(executor_server, "init_registry", lambda: None)
    monkeypatch.setattr(executor_server, "set_dumpable", lambda on: True)  # not the test process itself
    registry = ToolRegistry()
    terminal.register(registry)
    monkeypatch.setattr(executor_server, "_REGISTRY", registry)
    monkeypatch.setenv("TOOL_EXECUTOR_TOKEN", "secret-token")
    monkeypatch.setenv("TOOL_EXECUTOR_REQUIRE_FUSE", "0")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    if switch is None:
        monkeypatch.delenv("TOOL_EXECUTOR_ALLOW_LOCAL", raising=False)
    else:
        monkeypatch.setenv("TOOL_EXECUTOR_ALLOW_LOCAL", switch)
    executor_server.main()
    assert served["proxy_headers"] is False
    assert served["app"].openapi_url is None
    resp = await _post(served["app"], client=("127.0.0.1", 40000))
    assert resp.status_code == status


@pytest.mark.parametrize(
    ("peer", "local", "inside"),
    [
        ("127.0.0.1", "172.21.0.2", True), ("127.8.0.1", None, True), ("::1", "fd00::2", True),
        ("::ffff:127.0.0.1", None, True), ("172.21.0.2", "172.21.0.2", True), (None, "172.21.0.2", True),
        ("not-an-ip", None, True), ("198.51.100.1", "198.51.100.2", False), ("203.0.113.7", "203.0.113.9", False),
        ("fe80::1%eth0", "fe80::2", False),
    ],
)
def test_what_counts_as_inside(peer, local, inside):
    assert from_inside(peer, local) is inside


async def test_a_forwarded_for_header_from_loopback_does_not_make_a_call_outside(monkeypatch, tmp_path):
    """Through a real uvicorn server run as the daemon runs it: X-Forwarded-For must not rewrite the peer."""
    import asyncio

    import uvicorn

    registry = ToolRegistry()
    terminal.register(registry)
    monkeypatch.setattr(executor_server, "_REGISTRY", registry)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text(f"geesefs {workspace} fuse.geesefs rw 0 0\n")
    app = executor_server.create_app(
        token="secret-token", workspace=str(workspace), mounts_path=str(mounts), allow_local=False,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", proxy_headers=False))
    serve = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"http://127.0.0.1:{port}/execute", json={"name": "terminal", "args": {"command": "echo hi"}},
                headers={**AUTH, "X-Forwarded-For": "10.9.9.9", "Forwarded": "for=10.9.9.9"},
            )
        assert resp.status_code == 403
    finally:
        server.should_exit = True
        await serve


def test_the_daemons_switches_never_come_from_a_spec(tmp_path):
    from surogates.sandbox.base import SandboxSpec
    from surogates.sandbox.docker import DockerSandbox

    env = DockerSandbox()._build_env(
        SandboxSpec(env={"TOOL_EXECUTOR_ALLOW_LOCAL": "1", "FORWARDED_ALLOW_IPS": "*", "OK": "1"}),
        "sid", "tok", "ephemeral", None,
    )
    assert "TOOL_EXECUTOR_ALLOW_LOCAL" not in env and "FORWARDED_ALLOW_IPS" not in env and env["OK"] == "1"


def test_any_of_the_sandboxs_own_addresses_is_inside():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            own = s.getsockname()[0]
        except OSError:
            pytest.skip("no routable address here")
    assert from_inside(own, "203.0.113.9") is True


def test_the_daemon_is_not_dumpable_and_its_tool_childs_score_is_still_set():
    """Not dumpable, the daemon's environ is root's and it cannot be traced by its user; a fork of it writes its
    own oom score dumpable for that moment only."""
    import os
    import subprocess
    import sys

    code = (
        "import ctypes, os\n"
        "from surogates.sandbox.executor_server import set_dumpable\n"
        "from surogates.tools.utils.process_tree import go_first_out_of_memory\n"
        "assert set_dumpable(False)\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    set_dumpable(True); ok = go_first_out_of_memory(); set_dumpable(False)\n"
        "    print(ok, open('/proc/self/oom_score_adj').read().strip(), ctypes.CDLL(None).prctl(3, 0, 0, 0, 0), flush=True)\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
        "print(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}).stdout.split()
    assert out == ["True", "1000", "0", "0"]



def test_a_non_dumpable_tool_child_hands_parsers_a_copy_another_process_can_open(tmp_path):
    """A parser that converts through LibreOffice opens the file from another process: /proc/<pid>/fd of a
    non-dumpable process is refused to it, so the tool child hands it a private copy (live: .rtf/.odt/.doc failed
    with 'output PDF not found' before this)."""
    import os
    import subprocess
    import sys

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.rtf").write_bytes(b"{\\rtf1 hello}")
    code = (
        "import asyncio, os, subprocess\n"
        "from surogates.sandbox.executor_server import set_dumpable\n"
        "from surogates.tools.workspace_io.local import LocalWorkspaceIO\n"
        f"io = LocalWorkspaceIO(workspace_path={str(ws)!r})\n"
        "async def main(dumpable):\n"
        "    set_dumpable(dumpable)\n"
        f"    async with io.local_file({str(ws / 'a.rtf')!r}) as path:\n"
        "        out = subprocess.run(['cat', str(path)], capture_output=True)\n"
        "        print(dumpable, os.path.islink(path), out.returncode, out.stdout.decode())\n"
        "        print('mtime', path.stat().st_mtime_ns == os.stat(io.root + '/a.rtf').st_mtime_ns)\n"
        "asyncio.run(main(False)); asyncio.run(main(True))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    # The copy keeps the file's mtime: read_file's document cache is keyed by it.
    assert out.stdout.splitlines() == [
        "False False 0 {\\rtf1 hello}", "mtime True", "True True 0 {\\rtf1 hello}", "mtime True",
    ], out.stderr
