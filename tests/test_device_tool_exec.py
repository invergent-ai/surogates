"""Tool calls of a session on the user's computer reach it through the device, never this host."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.devices.sandbox import NOT_AVAILABLE, DeviceCall, enter_device_session, leave_device_session
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.harness.prompt import PromptBuilder
from surogates.harness.tool_exec import _build_session_sandbox_spec, execute_single_tool
from surogates.runtime.turn_slots import current_turn, turn_waiting
from surogates.sandbox.base import SandboxUnavailableError
from surogates.session.store import LeaseNotHeldError
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.workspace_io import LocalWorkspaceIO, workspace_io_from
from tests.test_turn_slots import held_turn

pytestmark = pytest.mark.asyncio

FOLDER = "/home/flavius/notes"
_ids = iter(range(1, 1_000_000))


def registry_with(*names: str, output: str = '{"ok": true}', max_result_size: int = 50_000) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(
            name,
            ToolSchema(
                name=name,
                description=name,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "action": {"type": "string"},
                        "pattern": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            ),
            handler=AsyncMock(return_value=output),
            max_result_size=max_result_size,
        )
    return registry


def device_session(**config) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        parent_id=None,
        agent_id="test-agent",
        model="test-model",
        config={
            "execution": {"kind": "device", "device_id": str(uuid4())},
            "workspace_path": FOLDER,
            **config,
        },
    )


def make_store() -> AsyncMock:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=lambda *a, **k: next(_ids))
    store.advance_harness_cursor = AsyncMock()
    return store


async def run(registry, session, name, args, *, sandbox_pool=None) -> dict:
    return await execute_single_tool(
        {"id": "call_1", "function": {"name": name, "arguments": json.dumps(args)}},
        session=session,
        lease=SimpleNamespace(lease_token=uuid4()),
        store=make_store(),
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
        sandbox_pool=sandbox_pool,
        session_factory=MagicMock(),
        redis=MagicMock(),
    )


async def test_a_sandbox_tool_runs_in_the_worker_on_the_device():
    registry = registry_with("read_file")
    session = device_session()
    pool = SimpleNamespace(ensure=AsyncMock(), execute=AsyncMock())
    await run(registry, session, "read_file", {"path": "notes.md"}, sandbox_pool=pool)
    kwargs = registry.get("read_file").handler.call_args.kwargs
    assert isinstance(kwargs["workspace_io"], DeviceWorkspaceIO)
    assert kwargs["workspace_io"].root == FOLDER
    assert kwargs["task_id"] == str(session.id)
    pool.ensure.assert_not_called()
    pool.execute.assert_not_called()


async def test_an_image_is_read_through_the_device_like_any_file():
    registry = registry_with("read_file")
    await run(registry, device_session(), "read_file", {"path": "photo.png"})
    kwargs = registry.get("read_file").handler.call_args.kwargs
    assert isinstance(kwargs["workspace_io"], DeviceWorkspaceIO)


async def test_a_harness_tool_gets_the_device_and_no_host_path():
    registry = registry_with("process")
    session = device_session()
    await run(registry, session, "process", {"action": "list"}, sandbox_pool=SimpleNamespace())
    kwargs = registry.get("process").handler.call_args.kwargs
    assert isinstance(kwargs["workspace_io"], DeviceWorkspaceIO)
    assert isinstance(kwargs["sandbox_pool"], DeviceCall)
    assert kwargs["workspace_path"] is None
    assert kwargs["task_id"] == str(session.id)


async def test_a_child_works_under_its_roots_task_id():
    root = str(uuid4())
    registry = registry_with("process")
    session = device_session(sandbox_root_session_id=root)
    await run(registry, session, "process", {"action": "list"})
    kwargs = registry.get("process").handler.call_args.kwargs
    assert kwargs["task_id"] == root
    # It journals as itself, under its root.
    runner = kwargs["workspace_io"]._runner
    assert (runner._root_session_id, runner._calling_session_id) == (UUID(root), session.id)


@pytest.mark.parametrize("name", ["run_coding_agent", "idea_tree", "dispatch_experiments", "merge_experiment"])
async def test_a_feature_switched_off_on_a_local_folder_is_refused(name):
    registry = registry_with(name)
    result = await run(registry, device_session(), name, {})
    assert NOT_AVAILABLE in json.loads(result["content"])["error"]
    registry.get(name).handler.assert_not_called()


async def test_containment_is_left_to_the_computer():
    # The governance containment check resolves paths on this host, where it
    # would refuse this one; the computer enforces its own folder instead
    # (and refuses it there).
    registry = registry_with("read_file")
    await run(registry, device_session(), "read_file", {"path": "../elsewhere/a.md"})
    registry.get("read_file").handler.assert_called_once()


async def test_an_oversized_result_spills_onto_the_computer():
    registry = registry_with("search_files", output="x" * 150_000, max_result_size=200_000)
    registry.register(
        "write_file",
        ToolSchema(name="write_file", description="write", parameters={"type": "object", "properties": {}}),
        handler=AsyncMock(return_value='{"bytes_written": 1}'),
    )
    await run(registry, device_session(), "search_files", {"pattern": "x"})
    write = registry.get("write_file").handler
    assert write.call_args.args[0]["path"].startswith(".surogates-results/")
    assert isinstance(write.call_args.kwargs["workspace_io"], DeviceWorkspaceIO)
    # The spill reuses the runner of the call that produced the result.
    assert write.call_args.kwargs["workspace_io"] is registry.get("search_files").handler.call_args.kwargs["workspace_io"]


async def test_a_committed_result_marks_what_the_call_read_consumed_after_the_commit(monkeypatch):
    done: list[str] = []

    async def consumed(self) -> None:
        done.append("consumed")

    def emit(session_id, kind, data, **fence) -> int:
        done.append(f"emit {kind.value}")
        return next(_ids)

    monkeypatch.setattr(DeviceCall, "consumed", consumed)
    store = make_store()
    store.emit_event = AsyncMock(side_effect=emit)
    await execute_single_tool(
        {"id": "call_1", "function": {"name": "read_file", "arguments": json.dumps({"path": "a.pdf"})}},
        session=device_session(),
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry_with("read_file"),
        tenant=MagicMock(asset_root="/tmp/test"),
        session_factory=MagicMock(),
        redis=MagicMock(),
    )
    assert done[-2:] == ["emit tool.result", "consumed"]


async def test_a_result_that_is_not_committed_leaves_what_the_call_read_for_the_next_worker(monkeypatch):
    consumed = AsyncMock()
    monkeypatch.setattr(DeviceCall, "consumed", consumed)

    def emit(session_id, kind, data, **fence) -> int:
        if kind.value == "tool.result":
            raise LeaseNotHeldError("another worker runs this session now")
        return next(_ids)

    store = make_store()
    store.emit_event = AsyncMock(side_effect=emit)
    with pytest.raises(asyncio.CancelledError):
        await execute_single_tool(
            {"id": "call_1", "function": {"name": "read_file", "arguments": json.dumps({"path": "a.pdf"})}},
            session=device_session(),
            lease=SimpleNamespace(lease_token=uuid4()),
            store=store,
            tools=registry_with("read_file"),
            tenant=MagicMock(asset_root="/tmp/test"),
            session_factory=MagicMock(),
            redis=MagicMock(),
        )
    consumed.assert_not_called()


async def test_a_local_folder_session_never_gets_a_cloud_sandbox_spec():
    with pytest.raises(SandboxUnavailableError, match="computer"):
        await _build_session_sandbox_spec(device_session(), MagicMock(), "owner")


async def test_nothing_in_a_local_folder_wake_falls_back_to_this_host():
    # The expert loop's harness tools arrive with no session config at all.
    token = enter_device_session(device_session())
    try:
        with pytest.raises(RuntimeError, match="computer"):
            workspace_io_from({"workspace_path": "/tmp"})
    finally:
        leave_device_session(token)


async def test_a_local_folder_session_never_falls_back_to_this_hosts_files():
    with pytest.raises(RuntimeError, match="computer"):
        workspace_io_from({
            "workspace_path": "/",
            "session_config": {"execution": {"kind": "device", "device_id": str(uuid4())}},
        })


async def test_a_cloud_session_still_gets_its_workspace_on_this_host(tmp_path):
    wio = workspace_io_from({"workspace_path": str(tmp_path), "session_config": {}})
    assert isinstance(wio, LocalWorkspaceIO)


async def test_project_files_are_not_read_from_this_host_for_a_local_folder(tmp_path):
    (tmp_path / "AGENTS.md").write_text("HOST ONLY")

    def context(config: dict) -> str:
        session = SimpleNamespace(config=config, channel="web")
        return PromptBuilder(tenant=MagicMock(), session=session)._context_files_section()

    assert "HOST ONLY" in context({"workspace_path": str(tmp_path)})
    assert "HOST ONLY" not in context({
        "workspace_path": str(tmp_path),
        "execution": {"kind": "device", "device_id": str(uuid4())},
    })


async def test_a_tool_call_is_activity_of_its_turn():
    slots, semaphore, gate = await held_turn()
    seen: list[bool] = []

    async def handler(arguments, **kwargs):
        async with turn_waiting():
            seen.append(semaphore.locked())
        return '{"ok": true}'

    registry = ToolRegistry()
    registry.register(
        "read_file",
        ToolSchema(name="read_file", description="read", parameters={"type": "object", "properties": {}}),
        handler=handler,
    )
    token = current_turn.set(slots)
    try:
        # As the loop runs it: the turn's own activity, joining while it waits for the call.
        async with slots.activity():
            async with slots.joining():
                await execute_single_tool(
                    {"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}},
                    session=SimpleNamespace(id=uuid4(), parent_id=None, agent_id="a", model="m", config={}),
                    lease=SimpleNamespace(lease_token=uuid4()),
                    store=make_store(),
                    tools=registry,
                    tenant=MagicMock(asset_root="/tmp/test"),
                )
    finally:
        current_turn.reset(token)
    # The lone tool call waited, so all of the turn waited and gave its slot back.
    assert seen == [False]
    assert semaphore.locked() and gate.held == 1
