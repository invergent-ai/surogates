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
    ok = {"server_relative_url": f"{base}/a.pdf", "name": "b.pdf"}
    assert inside_scratch("SHARE_POINT_UPDATE_DRIVE_ITEM", ok, SCOPE)
    for bad in ("/Documente/a.pdf", base + "X/a.pdf", base.lower() + "/a.pdf"):
        assert not inside_scratch("SHARE_POINT_UPDATE_DRIVE_ITEM", {**ok, "server_relative_url": bad}, SCOPE)


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
