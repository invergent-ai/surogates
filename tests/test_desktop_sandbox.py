"""Sandbox requests of a session on the user's computer: run there inside a tool call, refused outside one."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.devices.sandbox import (
    DEVICE_SANDBOX_ID,
    NOT_AVAILABLE,
    DeviceCall,
    device_call_for,
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
        read_tracker_id="root-1",
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


async def test_long_terminal_output_spills_onto_the_computer_not_this_host(laptop, monkeypatch):
    monkeypatch.setattr("surogates.tools.builtin.terminal.get_max_bytes", lambda: 1000)
    host_spills = lambda: set(Path(tempfile.gettempdir()).glob("terminal-output-*.log"))  # noqa: E731
    before = host_spills()

    command = "python3 -c \"print('x' * 60000)\""
    reply = json.loads(await laptop.call.execute("any", "terminal", json.dumps({"command": command})))

    assert ".surogates-results/terminal-output-" in reply["output"], reply
    [spill] = (laptop.folder / ".surogates-results").glob("terminal-output-*.log")
    assert spill.read_text().strip() == "x" * 60000
    assert host_spills() == before


@pytest.mark.parametrize("arguments", ["[]", "null"])
async def test_arguments_that_are_not_an_object_are_an_error_not_a_crash(laptop, arguments):
    reply = json.loads(await laptop.call.execute("any", "read_file", arguments))
    assert "JSON object" in reply["error"]
    assert laptop.runner.kinds == []


async def test_a_call_names_its_computer_and_a_resumed_one_skips_the_document_cache():
    session = device_session()

    def call(**resumed) -> DeviceCall:
        return device_call_for(
            session, tools=ToolRegistry(), invocation_id="1:call_1", lease_token=None,
            session_factory=None, redis=None, **resumed,
        )

    computer = f"device:{session.config['execution']['device_id']}"
    assert (call().workspace_io.identity, call().workspace_io.caches_documents) == (computer, True)
    # A hit would ask the computer for less than the first run did, and the call would read as interrupted.
    resumed = call(resumed=True).workspace_io
    assert (resumed.identity, resumed.caches_documents) == (computer, False)


async def test_only_a_session_on_the_computer_is_told_what_git_and_shared_mappings_cannot_do_in_its_folder():
    from surogates.harness.tool_schemas import describe_for_device
    from surogates.tools.builtin.terminal import DEVICE_GIT_NOTE, DEVICE_MAPPING_NOTE, TERMINAL_TOOL_DESCRIPTION

    tools = ToolRegistry()
    ToolRuntime(tools).register_builtins()
    schemas = tools.get_schemas()

    def terminal(described: list[dict]) -> str:
        return next(s["function"]["description"] for s in described if s["function"]["name"] == "terminal")

    assert terminal(describe_for_device(schemas, device_session().config)) == (
        f"{TERMINAL_TOOL_DESCRIPTION}{DEVICE_GIT_NOTE}\n{DEVICE_MAPPING_NOTE}\n"
    )
    assert "git init" in DEVICE_GIT_NOTE
    # The guest serves a folder uncached and refuses a shared mapping of its files, as SQLite's WAL makes,
    # and no lock crosses the share: a rollback journal is safe only while the user's computer leaves it alone.
    assert "WAL" in DEVICE_MAPPING_NOTE and "in your home folder" in DEVICE_MAPPING_NOTE
    assert "rollback journal only while nothing on the user's computer has it open" in DEVICE_MAPPING_NOTE
    assert "including one a failed attempt switched, cannot be opened there" in DEVICE_MAPPING_NOTE
    for cloud in ({}, None, {"execution": {"kind": "cloud"}}):
        assert terminal(describe_for_device(schemas, cloud)) == TERMINAL_TOOL_DESCRIPTION
    # The registry's schema, which every session shares, keeps the cloud's text.
    assert terminal(schemas) == TERMINAL_TOOL_DESCRIPTION


async def test_only_a_session_on_the_computer_is_offered_the_browsers_upload():
    from surogates.harness.tool_schemas import describe_for_device

    tools = ToolRegistry()
    ToolRuntime(tools).register_builtins()
    schemas = tools.get_schemas()

    def names(described: list[dict]) -> set[str]:
        return {s["function"]["name"] for s in described}

    assert "browser_upload_file" in names(describe_for_device(schemas, device_session().config))
    for cloud in ({}, None, {"execution": {"kind": "cloud"}}):
        assert "browser_upload_file" not in names(describe_for_device(schemas, cloud))
        assert "browser_navigate" in names(describe_for_device(schemas, cloud))


@pytest.mark.parametrize("streamed", [False, True], ids=["sequential", "streamed"])
async def test_a_session_in_the_cloud_cannot_call_the_browsers_upload_it_was_not_offered(monkeypatch, streamed):
    from surogates.harness import loop
    from tests.test_steer_loop import _final_response, _make_loop_harness, _make_session

    tools = ToolRegistry()
    ToolRuntime(tools).register_builtins()
    ran = AsyncMock(return_value='{"uploaded": 1}')
    monkeypatch.setattr(tools, "dispatch", ran)
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(session_store=store)
    harness._tools = tools
    harness._streaming_enabled = streamed
    call = {
        "id": "call_upload", "type": "function",
        "function": {"name": "browser_upload_file", "arguments": json.dumps({"paths": ["report.pdf"]})},
    }
    answers = iter([
        ({"role": "assistant", "content": "", "tool_calls": [call]},
         {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1}),
        _final_response("Done."),
    ])
    offered: list[set[str]] = []

    async def model(**kwargs):
        offered.append({schema["function"]["name"] for schema in kwargs["create_kwargs"]["tools"]})
        message, usage = next(answers)
        if kwargs["on_tool_call_complete"] is not None:
            for made in message["tool_calls"] or []:
                kwargs["on_tool_call_complete"](made)
        return message, usage

    monkeypatch.setattr(loop, "call_llm_with_retry", model)
    messages = [{"role": "user", "content": "Give the page the report"}]
    await harness._run_loop(_make_session(), messages, "system", SimpleNamespace(lease_token=uuid4()), all_events=[])

    # The cloud's browser tools are there; the upload is not, and its name alone runs nothing.
    assert "browser_navigate" in offered[0] and "browser_upload_file" not in offered[0]
    ran.assert_not_awaited()
    [answer] = [message["content"] for message in messages if message.get("role") == "tool"]
    assert json.loads(answer)["error"].startswith("Unknown tool: 'browser_upload_file'. Available tools: ")
