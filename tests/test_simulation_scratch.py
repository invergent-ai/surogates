"""A test chat's changes run for real only inside its scratch copy."""

from __future__ import annotations

import pytest

from surogates.simulation import side_effects
from surogates.simulation.scratch import inside_scratch
from surogates.simulation.side_effects import decide, is_simulated

ROUTER = "mcp__tool_router__COMPOSIO_MULTI_EXECUTE_TOOL"
SCOPE = {"drive_id": "D", "root_id": "R1", "root_path": "/Surogate - teste/r1/0-1/Fise", "item_ids": ["F1", "F2"]}
RENAME = {"itemId": "F1", "parentReference": {"id": "R1", "driveId": "D"}, "name": "x.pdf"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    side_effects._decided.clear()


def call(slug, arguments):
    return {"tools": [{"tool_slug": slug, "arguments": arguments}]}


def test_rename_inside_copy_is_real():
    assert inside_scratch("ONE_DRIVE_MOVE_ITEM", RENAME, SCOPE)


def test_move_out_of_copy_is_faked():
    moved = {**RENAME, "parentReference": {"id": "ORIG", "driveId": "D"}}
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", moved, SCOPE)


def test_unknown_reference_key_is_faked():
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", {**RENAME, "targetFolderId": "ORIG"}, SCOPE)
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", {**RENAME, "target": "01TVGB2LABCDEFGHIJKLMN"}, SCOPE)
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", {**RENAME, "target": "/a/b"}, SCOPE)


def test_other_drive_is_faked():
    other = {**RENAME, "parentReference": {"id": "R1", "driveId": "D2"}}
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", other, SCOPE)
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", {**RENAME, "drive_id": "D2"}, SCOPE)


def test_sharepoint_path_outside_root_is_faked():
    base = "/Surogate - teste/r1/0-1/Fise"
    ok = {"server_relative_url": f"{base}/a.pdf", "name": "b.pdf", "drive_id": "D"}
    assert inside_scratch("SHARE_POINT_UPDATE_DRIVE_ITEM", ok, SCOPE)
    for bad in ("/Documente/a.pdf", base + "X/a.pdf", base.lower() + "/a.pdf",
                base + "/../../../../Documente/a.pdf", base + "/%2e%2e/%2e%2e/x", base + "/./a.pdf", base + "\\..\\x"):
        assert not inside_scratch("SHARE_POINT_UPDATE_DRIVE_ITEM", {**ok, "server_relative_url": bad}, SCOPE)


def test_onedrive_folder_path_traversal_is_faked():
    base = SCOPE["root_path"]
    up = {"folder_path": base + "/sub", "name": "a", "drive_id": "D"}
    assert inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {**up, "folder_path": base + "/../x"}, SCOPE)


def test_path_without_drive_is_faked():
    up = {"folder_path": SCOPE["root_path"] + "/sub", "name": "a"}
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", up, SCOPE)


def test_not_a_file_action_or_no_scope_is_faked():
    assert not inside_scratch("OUTLOOK_SEND_EMAIL", RENAME, SCOPE)
    assert not inside_scratch("ONE_DRIVE_MOVE_ITEM", RENAME, {})
    assert not inside_scratch("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"name": "a.pdf"}, SCOPE)


@pytest.mark.asyncio
async def test_rename_inside_copy_runs_through_decide():
    assert not await decide(ROUTER, None, llm=None, args=call("ONE_DRIVE_MOVE_ITEM", RENAME), scratch=SCOPE)
    assert await decide(ROUTER, None, llm=None, args=call("ONE_DRIVE_MOVE_ITEM", RENAME), scratch=None)


@pytest.mark.asyncio
async def test_email_is_always_faked():
    assert await decide(ROUTER, None, llm=None, args=call("OUTLOOK_SEND_EMAIL", RENAME), scratch=SCOPE)


@pytest.mark.asyncio
async def test_workbench_is_faked_in_test_mode():
    assert await decide("mcp__tool_router__REMOTE_WORKBENCH", None, llm=None, args={}, scratch=SCOPE)


@pytest.mark.asyncio
async def test_execute_mixed_batch_is_faked():
    args = {"tools": [
        {"tool_slug": "ONE_DRIVE_MOVE_ITEM", "arguments": RENAME},
        {"tool_slug": "ONE_DRIVE_DELETE_ITEM", "arguments": {"item_id": "FOREIGN"}},
        {"tool_slug": "ONE_DRIVE_LIST_ITEMS", "arguments": {}},
    ]}
    assert await decide(ROUTER, None, llm=None, args=args, scratch=SCOPE)
    args["tools"].pop(1)
    assert not await decide(ROUTER, None, llm=None, args=args, scratch=SCOPE)


def test_test_mode_counts_as_simulated():
    assert is_simulated({"test_mode": {"scratch": None}})


@pytest.mark.asyncio
async def test_junk_batch_entry_is_faked_not_a_crash():
    good = {"tool_slug": "ONE_DRIVE_DELETE_ITEM", "arguments": {"item_id": "F1"}}
    assert not await decide(ROUTER, None, llm=None, args={"tools": [good]}, scratch=SCOPE)
    for junk in ("junk", {"arguments": {}}, {"tool_slug": "ONE_DRIVE_DELETE_ITEM", "arguments": "x"}):
        assert await decide(ROUTER, None, llm=None, args={"tools": [good, junk]}, scratch=SCOPE)


# --- the agent's own platform-state tools ------------------------------------


async def _run_builtin(name: str, config: dict, handler):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from surogates.harness.tool_exec import execute_single_tool
    from surogates.session.events import EventType
    from surogates.tools.registry import ToolRegistry, ToolSchema

    registry = ToolRegistry()
    registry.register(
        name,
        ToolSchema(name=name, description=name, parameters={"type": "object", "properties": {}}),
        handler=handler,
        toolset="core",
    )
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=[1, 2])
    store.advance_harness_cursor = AsyncMock()
    session = SimpleNamespace(id=uuid4(), config={"workspace_path": "", **config}, agent_id="a")
    result = await execute_single_tool(
        {"id": "c1", "function": {"name": name, "arguments": "{}"}},
        session=session,
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=SimpleNamespace(asset_root="/tmp/test"),
    )
    emitted = store.emit_event.await_args_list[-1].args
    assert emitted[1] == EventType.TOOL_RESULT
    return json.loads(result["content"]) if result["content"].startswith("{") else result["content"], emitted[2]


def _state_tool_names():
    from surogates.simulation.scratch import STATE_TOOLS

    return sorted(STATE_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _state_tool_names())
async def test_state_tools_are_faked_in_test_mode(name):
    from unittest.mock import AsyncMock

    handler = AsyncMock(return_value='{"saved": true}')
    result, event = await _run_builtin(name, {"test_mode": {"scratch": None}}, handler)
    handler.assert_not_awaited()
    assert result == {"ok": True}
    assert event["simulated"]["would_have"].startswith(f"would have called {name}")


@pytest.mark.asyncio
async def test_state_tools_run_for_real_outside_test_mode_and_reads_stay_real():
    from unittest.mock import AsyncMock

    from surogates.simulation.scratch import STATE_TOOLS

    handler = AsyncMock(return_value='{"saved": true}')
    _, event = await _run_builtin("memory", {}, handler)
    handler.assert_awaited_once()
    assert "simulated" not in event
    for read in ("kb_search_pages", "cron_list", "skill_view", "read_file", "ask_user_question", "write_file"):
        assert read not in STATE_TOOLS
        handler = AsyncMock(return_value='{"ok": 1}')
        await _run_builtin(read, {"test_mode": {"scratch": None}}, handler)
        handler.assert_awaited_once()
