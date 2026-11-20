"""A timed-out or cancelled command is stopped with everything it started.

Before, a terminal timeout killed only the shell, and the sandbox daemon's
timeout only its forked tool child: a script the command had started ran on,
reparented to the container's init (on 11-20 one grew ~6 GB a minute for 70
minutes after its call had timed out).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from surogates.sandbox import executor_server
from surogates.tools.builtin import terminal
from surogates.tools.registry import ToolRegistry
from surogates.tools.utils import process_tree
from surogates.tools.utils.process_tree import descendants, start_time, stop_process_tree
from surogates.tools.workspace_io.local import _run_command

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="process trees are read from /proc (Linux)")

AUTH = {"Authorization": "Bearer secret-token"}


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _gone_within(pids: list[int], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


def _pids(path: Path, count: int, seconds: float = 10.0) -> list[int]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            pids = [int(p) for p in path.read_text().split()]
            if len(pids) >= count:
                return pids
        time.sleep(0.05)
    raise AssertionError(f"{path} never had {count} pids")


class _Reap(list):
    """Whatever a test started, gone at its end even when it fails: each pid with its start time, so a pid
    taken meanwhile by another process is left alone."""

    def append(self, pid: int) -> None:
        super().append((pid, start_time(pid)))

    def __iadd__(self, pids):
        for pid in pids:
            self.append(pid)
        return self

    def extend(self, pids) -> None:
        self.__iadd__(pids)


@pytest.fixture()
def reap():
    started = _Reap()
    yield started
    for pid, start in started:
        if start is not None and start_time(pid) == start:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


# --- the tree stop ---------------------------------------------------------------------------------------------


def test_a_tree_is_stopped_children_and_grandchildren(tmp_path, reap):
    pidfile = tmp_path / "pids"
    shell = subprocess.Popen(
        ["sh", "-c", f"sleep 300 & echo $! >> {pidfile}; sh -c 'sleep 300 & echo $! >> {pidfile}; wait' & wait"],
        start_new_session=True,
    )
    pids = _pids(pidfile, 2)
    reap += [shell.pid, *pids]
    assert set(pids) <= set(descendants(shell.pid))
    stop_process_tree(shell.pid, session=shell.pid, grace=1.0)
    shell.wait(timeout=5)
    assert _gone_within(pids, 2)


def test_a_process_that_left_the_session_is_stopped_with_the_tree(tmp_path, reap):
    """``setsid`` takes a grandchild out of the command's session: the walk of the tree still finds it."""
    pidfile = tmp_path / "pids"
    shell = subprocess.Popen(
        ["sh", "-c", f"setsid sh -c 'echo $$ >> {pidfile}; exec sleep 300' & wait"], start_new_session=True,
    )
    [escaped] = _pids(pidfile, 1)
    reap += [shell.pid, escaped]
    assert os.getsid(escaped) != shell.pid
    stop_process_tree(shell.pid, session=shell.pid, grace=1.0)
    assert _gone_within([escaped], 2)


def test_a_double_fork_under_a_subreaper_is_stopped_with_it(tmp_path, reap):
    """``( setsid sleep & )``: the subshell ends at once, and its child leaves the session and the shell's tree.
    Under a subreaper (the daemon's tool child) it is reparented to that, where the stop finds it."""
    pidfile = tmp_path / "pids"
    code = (
        "import os, subprocess, sys, time\n"
        "from surogates.tools.utils.process_tree import become_subreaper, stop_process_tree\n"
        "assert become_subreaper()\n"
        f"subprocess.run(['sh', '-c', '( setsid sleep 300 > /dev/null 2>&1 & echo $! > {pidfile} )'])\n"
        f"while not os.path.exists({str(pidfile)!r}): time.sleep(0.05)\n"
        "time.sleep(0.3)\n"
        "stop_process_tree(os.getpid(), include_root=False, grace=1.0)\n"
        "print('stopped')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    [escaped] = _pids(pidfile, 1)
    reap.append(escaped)
    assert out.stdout.strip() == "stopped", out.stderr
    assert _gone_within([escaped], 2)


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace(tmp_path, reap):
    pidfile = tmp_path / "pids"
    code = f"import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(300)"
    shell = subprocess.Popen(["sh", "-c", f"{sys.executable} -c \"{code}\"; true"], start_new_session=True)
    [stubborn] = _pids(pidfile, 1)
    reap += [shell.pid, stubborn]
    started = time.monotonic()
    stop_process_tree(shell.pid, session=shell.pid, grace=0.5)
    assert _gone_within([stubborn], 2)
    assert time.monotonic() - started >= 0.5


def test_the_caller_and_its_own_session_are_never_stopped():
    """In a throwaway child: a broken guard would stop the caller, here not the test run."""
    code = (
        "import os\n"
        "from surogates.tools.utils.process_tree import stop_process_tree\n"
        "os.setsid()\n"
        "stop_process_tree(os.getpid(), session=os.getsid(0), grace=0.1)\n"
        "print('alive')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert out.stdout.strip() == "alive", out.stderr


def test_a_root_whose_pid_was_taken_is_not_walked_from(monkeypatch):
    """The shell ended and its pid went to another process: that one and its children are left alone."""
    sent = []
    monkeypatch.setattr(process_tree, "_kill", lambda pid, sig: sent.append((pid, sig)))
    other = subprocess.Popen(["sleep", "30"])
    try:
        stop_process_tree(other.pid, root_start=(start_time(other.pid) or 0) - 1, session=other.pid, grace=0.1)
        assert sent == []
        # A root that had ended when it was looked for: nothing either.
        stop_process_tree(other.pid, root_start=None, session=other.pid, grace=0.1)
        assert sent == []
    finally:
        monkeypatch.undo()
        other.kill()
        other.wait()


def test_a_reused_pid_is_not_signalled(monkeypatch):
    """A pid is signalled only while it is the process that started when the walk saw it."""
    sent = []
    monkeypatch.setattr(process_tree, "_stat", lambda pid: ("S", 1, 1, 222))
    monkeypatch.setattr(process_tree, "_kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(process_tree, "_getpid", lambda: 1)
    process_tree._signal({4242: 111}, signal.SIGKILL)
    assert sent == []
    process_tree._signal({4242: 222}, signal.SIGKILL)
    assert sent == [(4242, signal.SIGKILL)]


# --- the terminal's own timeout, and a cancel ------------------------------------------------------------------


async def test_a_terminal_timeout_stops_what_the_command_started(tmp_path, reap):
    pidfile = tmp_path / "pids"
    command = f"python3 -c 'import time; time.sleep(300)' & echo $! > {pidfile}; wait"
    began = time.monotonic()
    result = await _run_command(command, cwd=str(tmp_path), timeout=1, env=dict(os.environ))
    # On master the call came back only once the grandchild holding its pipe ended by itself.
    assert time.monotonic() - began < 10
    assert result.timed_out and result.returncode == 124
    [grandchild] = _pids(pidfile, 1)
    reap.append(grandchild)
    assert _gone_within([grandchild], 1)


async def test_a_cancelled_command_is_stopped_with_what_it_started(tmp_path, reap):
    pidfile = tmp_path / "pids"
    command = f"sleep 300 & echo $! > {pidfile}; wait"
    task = asyncio.ensure_future(_run_command(command, cwd=str(tmp_path), timeout=300, env=dict(os.environ)))
    [grandchild] = await asyncio.to_thread(_pids, pidfile, 1)
    reap.append(grandchild)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(_gone_within, [grandchild], 4)


async def test_a_command_that_finishes_leaves_its_detached_background_running(tmp_path, reap):
    """``nohup ... &`` with its output elsewhere is started on purpose: a call that returns does not stop it."""
    pidfile = tmp_path / "pids"
    command = f"nohup sleep 300 > /dev/null 2>&1 & echo $! > {pidfile}"
    result = await _run_command(command, cwd=str(tmp_path), timeout=10, env=dict(os.environ))
    assert result.returncode == 0 and not result.timed_out
    [server] = _pids(pidfile, 1)
    reap.append(server)
    await asyncio.sleep(0.5)
    assert _alive(server)


# --- the sandbox daemon's timeout ------------------------------------------------------------------------------


@pytest.fixture()
def daemon(monkeypatch, tmp_path):
    registry = ToolRegistry()
    terminal.register(registry)
    monkeypatch.setattr(executor_server, "_REGISTRY", registry)
    mounts = tmp_path / "mounts"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mounts.write_text(f"geesefs {workspace} fuse.geesefs rw 0 0\n")
    app = executor_server.create_app(token="secret-token", workspace=str(workspace), mounts_path=str(mounts))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://daemon")


async def test_the_daemons_timeout_stops_the_tool_child_and_what_its_command_started(daemon, tmp_path, reap):
    """The 11-20 case: the terminal's own timeout was longer than the call's, so the daemon's fired first."""
    pidfile = tmp_path / "pids"
    # A grandchild that grows (here: sleeps) and one that a double fork took out of the shell's tree.
    command = (
        f"python3 -c 'import time; time.sleep(300)' & echo $! >> {pidfile}; "
        f"( sleep 300 & echo $! >> {pidfile} ); wait"
    )
    async with daemon as client:
        resp = await client.post(
            "/execute", headers=AUTH,
            json={"name": "terminal", "args": {"command": command, "timeout": 600}, "timeout": 2},
        )
    assert json.loads(resp.text)["timed_out"] is True
    pids = _pids(pidfile, 2)
    reap.extend(pids)
    assert await asyncio.to_thread(_gone_within, pids, 2)


async def test_in_the_daemon_a_terminal_timeout_stops_a_double_fork_too(daemon, tmp_path, reap):
    """The terminal's own timeout, under the daemon's longer one: a ``( setsid ... & )`` escape went to the tool
    child (the subreaper), out of the shell's tree and session, and is stopped all the same."""
    pidfile = tmp_path / "pids"
    command = f"( setsid sleep 300 & echo $! > {pidfile} ); sleep 300"
    async with daemon as client:
        resp = await client.post(
            "/execute", headers=AUTH,
            json={"name": "terminal", "args": {"command": command, "timeout": 1}, "timeout": 60},
        )
    assert json.loads(resp.text)["exit_code"] == 124
    [escaped] = _pids(pidfile, 1)
    reap.append(escaped)
    assert await asyncio.to_thread(_gone_within, [escaped], 3)


async def test_in_the_daemon_a_call_that_returns_leaves_its_detached_server_running(daemon, tmp_path, reap):
    pidfile = tmp_path / "pids"
    command = f"nohup sleep 300 > /dev/null 2>&1 & echo $! > {pidfile}"
    async with daemon as client:
        resp = await client.post("/execute", headers=AUTH, json={"name": "terminal", "args": {"command": command}})
    assert json.loads(resp.text)["exit_code"] == 0
    [server] = _pids(pidfile, 1)
    reap.append(server)
    await asyncio.sleep(0.5)
    assert _alive(server)


async def test_a_call_whose_caller_hangs_up_is_stopped_with_what_its_command_started(monkeypatch, tmp_path, reap):
    """A stopped agent's call: the harness hangs up, and the command it ran stops with what it started."""
    import uvicorn

    registry = ToolRegistry()
    terminal.register(registry)
    monkeypatch.setattr(executor_server, "_REGISTRY", registry)
    mounts = tmp_path / "mounts"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mounts.write_text(f"geesefs {workspace} fuse.geesefs rw 0 0\n")
    app = executor_server.create_app(token="secret-token", workspace=str(workspace), mounts_path=str(mounts))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    serve_task = asyncio.create_task(server.serve())
    pidfile = tmp_path / "pids"
    command = f"sleep 300 & echo $! > {pidfile}; wait"
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/execute"
        async with httpx.AsyncClient() as client:
            call = asyncio.ensure_future(client.post(
                url, headers=AUTH, json={"name": "terminal", "args": {"command": command, "timeout": 600}, "timeout": 300},
                timeout=60,
            ))
            [grandchild] = await asyncio.to_thread(_pids, pidfile, 1)
            reap.append(grandchild)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        assert await asyncio.to_thread(_gone_within, [grandchild], 5)
    finally:
        server.should_exit = True
        await serve_task


# --- the memory limit: commands go first -----------------------------------------------------------------------


async def test_a_tool_childs_commands_are_the_first_the_kernel_kills_out_of_memory(daemon):
    """At the sandbox's memory limit the kernel kills the runaway command, not the daemon or the workspace's mount."""
    before = Path("/proc/self/oom_score_adj").read_text().strip()
    async with daemon as client:
        resp = await client.post(
            "/execute", headers=AUTH, json={"name": "terminal", "args": {"command": "cat /proc/self/oom_score_adj"}},
        )
    assert json.loads(resp.text)["output"] == "1000"
    # The daemon (here: the test's process) keeps its own.
    assert Path("/proc/self/oom_score_adj").read_text().strip() == before


def test_the_daemons_background_commands_go_first_too():
    from surogates.tools.utils.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    registry.oom_first = True
    session = registry.spawn("sleep 0.5; cat /proc/$$/oom_score_adj", cwd="/tmp", task_id="t")
    result = registry.wait(session.id, timeout=10)
    assert result["output"].strip().splitlines()[-1] == "1000"
    # Elsewhere (a worker's own commands, a computer's) nothing changes: the command has its starter's.
    plain = ProcessRegistry()
    session = plain.spawn("sleep 0.5; cat /proc/$$/oom_score_adj", cwd="/tmp", task_id="t")
    own = Path("/proc/self/oom_score_adj").read_text().strip()
    assert plain.wait(session.id, timeout=10)["output"].strip().splitlines()[-1] == own
