"""Sandbox requests of a session on the user's computer: run there inside a tool call, refused outside one."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.devices.sandbox import (
    DEVICE_SANDBOX_ID,
    NOT_AVAILABLE,
    DeviceCall,
    enter_device_session,
    leave_device_session,
    refusal,
)
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.sandbox.base import SandboxStatus, default_sandbox_spec
from surogates.sandbox.pool import SandboxPool
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import InProcessRunner

pytestmark = pytest.mark.asyncio


class RecordingBackend:
    """A cloud sandbox backend that records what it is asked to do."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def provision(self, spec) -> str:
        self.calls.append("provision")
        return "sb-1"

    async def execute(self, sandbox_id: str, name: str, input: str) -> str:
        self.calls.append(f"execute:{name}")
        return '{"ok": true}'

    async def destroy(self, sandbox_id: str) -> None:
        self.calls.append("destroy")

    async def status(self, sandbox_id: str) -> SandboxStatus:
        return SandboxStatus.RUNNING


def device_session(**config) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        parent_id=None,
        config={
            "execution": {"kind": "device", "device_id": str(uuid4())},
            "workspace_path": "/home/flavius/notes",
            **config,
        },
    )


@pytest.fixture
def laptop(tmp_path):
    folder = (tmp_path / "laptop").resolve()
    folder.mkdir()
    runner = InProcessRunner(LocalWorkspaceIO(str(folder)))
    tools = ToolRegistry()
    ToolRuntime(tools).register_builtins()
    call = DeviceCall(
        tools=tools,
        workspace_io=DeviceWorkspaceIO(runner, root="/home/flavius/notes"),
        task_id="root-1",
    )
    return SimpleNamespace(folder=folder, runner=runner, call=call)


async def test_a_sandbox_tool_runs_on_the_computer(laptop):
    output = await laptop.call.execute("any", "write_file", json.dumps({
        "path": "plan.md",
        "content": "draft\n",
        "_trace_context": {"trace_id": "t", "span_id": "s"},
    }))
    assert "error" not in json.loads(output), output
    assert (laptop.folder / "plan.md").read_text() == "draft\n"
    assert "write" in laptop.runner.kinds


async def test_a_call_is_ready_at_once_however_the_computer_is(laptop):
    assert await laptop.call.ensure("any", default_sandbox_spec()) == DEVICE_SANDBOX_ID
    assert laptop.runner.kinds == []


@pytest.mark.parametrize("name", ["_code", "_checkpoint"])
async def test_internal_sandbox_commands_are_refused_on_the_computer(laptop, name):
    reply = json.loads(await laptop.call.execute("any", name, "{}"))
    assert NOT_AVAILABLE in reply["error"]
    assert laptop.runner.kinds == []


async def test_a_refused_checkpoint_tells_the_saga_it_failed():
    assert json.loads(refusal("_checkpoint"))["success"] is False


async def test_the_pool_never_provisions_for_a_session_on_the_computer():
    backend = RecordingBackend()
    pool = SandboxPool(backend)
    session = device_session()
    token = enter_device_session(session)
    try:
        assert await pool.ensure(str(session.id), default_sandbox_spec()) == DEVICE_SANDBOX_ID
        reply = json.loads(await pool.execute(str(session.id), "terminal", json.dumps({"command": "ls"})))
    finally:
        leave_device_session(token)
    assert NOT_AVAILABLE in reply["error"]
    assert backend.calls == []


async def test_a_child_is_guarded_under_its_roots_key():
    root = str(uuid4())
    backend = RecordingBackend()
    pool = SandboxPool(backend)
    token = enter_device_session(device_session(sandbox_root_session_id=root))
    try:
        assert await pool.ensure(root, default_sandbox_spec()) == DEVICE_SANDBOX_ID
    finally:
        leave_device_session(token)
    assert backend.calls == []


async def test_other_sessions_still_get_a_cloud_sandbox():
    backend = RecordingBackend()
    pool = SandboxPool(backend)
    token = enter_device_session(device_session())
    try:
        await pool.ensure("someone-else", default_sandbox_spec())
        await pool.execute("someone-else", "terminal", "{}")
    finally:
        leave_device_session(token)
    assert backend.calls == ["provision", "execute:terminal"]


async def test_the_guard_ends_with_the_wake():
    backend = RecordingBackend()
    pool = SandboxPool(backend)
    session = device_session()
    leave_device_session(enter_device_session(session))
    await pool.ensure(str(session.id), default_sandbox_spec())
    assert backend.calls == ["provision"]


async def test_a_cloud_session_is_not_marked():
    assert enter_device_session(SimpleNamespace(id=uuid4(), parent_id=None, config={})) is None


async def test_malformed_arguments_are_an_error_not_a_crash(laptop):
    reply = json.loads(await laptop.call.execute("any", "read_file", "{not json"))
    assert "Invalid JSON arguments" in reply["error"]
