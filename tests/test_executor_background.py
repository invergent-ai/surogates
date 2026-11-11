"""A background command lives in the sandbox's executor daemon, and the daemon tells which have ended.

Each ``/execute`` forks a child that exits once its call returns: a
background command's process registry made there would die with it, the
command's output and exit with it, and ``process`` could never find it.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from surogates.sandbox import executor_server
from surogates.tools.builtin import terminal
from surogates.tools.registry import ToolRegistry

AUTH = {"Authorization": "Bearer secret-token"}


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


async def call(client: httpx.AsyncClient, name: str, **args) -> dict:
    resp = await client.post("/execute", json={"name": name, "args": args}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return json.loads(resp.text)


async def drained_until(client: httpx.AsyncClient, proc_id: str, seconds: float = 10.0) -> dict:
    """Drain the daemon until *proc_id*'s completion comes; every drain on the way names it as known."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        answer = await call(client, "_background")
        for event in answer["events"]:
            if event["session_id"] == proc_id:
                assert proc_id in answer["known"]
                return event
        assert proc_id in answer["known"]
        await asyncio.sleep(0.05)
    raise AssertionError(f"{proc_id} never reported its end")


async def test_a_background_command_outlives_its_call_and_its_end_is_drained_once(daemon):
    async with daemon as client:
        started = await call(
            client, "terminal", command="echo started; sleep 2; echo built; exit 3",
            background=True, notify_on_complete=True,
        )
        proc_id = started["session_id"]
        # Asked again later, in its own call, the daemon still has it running.
        polled = await call(client, "process", action="poll", session_id=proc_id)
        assert polled["status"] == "running"

        ended = await drained_until(client, proc_id)
        assert ended["exit_code"] == 3
        assert "built" in ended["output"]
        # Drained once: the next drain no longer carries it, but still knows it.
        again = await call(client, "_background")
        assert proc_id not in [event["session_id"] for event in again["events"]]
        assert proc_id in again["known"]
        # ``process`` reads its whole log, also after it ended.
        log = await call(client, "process", action="log", session_id=proc_id)
        assert "started" in log["output"] and "built" in log["output"]


async def test_a_command_that_does_not_ask_to_report_back_is_never_drained(daemon):
    async with daemon as client:
        started = await call(client, "terminal", command="exit 0", background=True)
        proc_id = started["session_id"]
        deadline = time.monotonic() + 5
        while (await call(client, "process", action="poll", session_id=proc_id))["status"] == "running":
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        answer = await call(client, "_background")
        assert proc_id not in [event["session_id"] for event in answer["events"]]


async def test_a_foreground_command_still_runs_in_a_fork(daemon, monkeypatch):
    forked: list[str] = []
    real = executor_server.execute_in_child

    async def watched(name, args, workspace, timeout, history=None):
        forked.append(name)
        return await real(name, args, workspace, timeout, history)

    monkeypatch.setattr(executor_server, "execute_in_child", watched)
    async with daemon as client:
        result = await call(client, "terminal", command="echo hi")
        assert result["output"] == "hi"
        await call(client, "terminal", command="exit 0", background=True)
        await call(client, "_background")
    assert forked == ["terminal"]


async def test_a_drain_is_answered_while_every_slot_runs_a_long_call(daemon):
    async with daemon as client:
        slow = [
            asyncio.create_task(call(client, "terminal", command="sleep 2"))
            for _ in range(executor_server.MAX_CONCURRENCY)
        ]
        await asyncio.sleep(0.3)
        started = time.monotonic()
        answer = await call(client, "_background")
        assert "known" in answer and time.monotonic() - started < 1.0
        await asyncio.gather(*slow)
