"""On local storage, the image tools read the folder the session's sandbox shows."""

from __future__ import annotations

import base64
import os
import uuid
from types import SimpleNamespace

import pytest

from surogates.harness.image_read import _build_key
from surogates.harness.loop import AgentHarness
from surogates.storage.backend import LocalBackend, S3Backend
from surogates.storage.tenant import sandbox_read_keys
from surogates.tools.builtin.vision import _image_ref_to_data_url
from surogates.tools.workspace_io import local

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
OTHER = b"\x89PNG\r\n\x1a\n" + b"1" * 16
BUCKET = "agent-bucket"


async def _read(storage, ref, session_id, config) -> bytes:
    url, _ = await _image_ref_to_data_url(
        ref, workspace_path=config.get("workspace_path"), storage=storage, session_id=session_id, session_config=config,
    )
    return base64.b64decode(url.split(",", 1)[1])


@pytest.fixture
def stock(tmp_path):
    storage = LocalBackend(str(tmp_path))
    session_id = str(uuid.uuid4())
    workspace = storage.resolve_workspace_path(BUCKET, session_id)
    return storage, session_id, {"storage_bucket": BUCKET, "workspace_path": workspace}


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["a.png", "/workspace/a.png", "pics/../a.png"])
async def test_stock_local_storage_reads_the_image_the_sandbox_shows(stock, tmp_path, ref) -> None:
    storage, session_id, config = stock
    (tmp_path / BUCKET / "sessions" / session_id / "a.png").write_bytes(PNG)
    if ".." in ref:
        with pytest.raises(Exception, match="traversal"):
            await _read(storage, ref, session_id, config)
        return
    assert await _read(storage, ref, session_id, config) == PNG


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", ["resolved", "through the link"])
async def test_a_stack_with_a_sessions_link_keeps_working(tmp_path, recorded) -> None:
    storage = LocalBackend(str(tmp_path))
    await storage.create_bucket(BUCKET)
    (tmp_path / BUCKET / "sessions").symlink_to(".")
    session_id = str(uuid.uuid4())
    workspace = storage.resolve_workspace_path(BUCKET, session_id)
    assert workspace == str((tmp_path / BUCKET / session_id).resolve())
    if recorded != "resolved":
        workspace = str(tmp_path / BUCKET / "sessions" / session_id)
    config = {"storage_bucket": BUCKET, "workspace_path": workspace}
    (tmp_path / BUCKET / session_id / "a.png").write_bytes(PNG)
    assert sandbox_read_keys(storage, BUCKET, config, session_id, "a.png") == [f"{session_id}/a.png"]
    assert await _read(storage, "/workspace/a.png", session_id, config) == PNG


@pytest.mark.asyncio
async def test_what_the_sandbox_shows_comes_before_the_sessions_own_key(stock, tmp_path) -> None:
    storage, session_id, config = stock
    (tmp_path / BUCKET / "sessions" / session_id / "a.png").write_bytes(PNG)
    await storage.write(BUCKET, f"{session_id}/a.png", OTHER)
    assert await _read(storage, "a.png", session_id, config) == PNG


@pytest.mark.asyncio
async def test_a_file_only_under_the_sessions_key_is_still_found(stock) -> None:
    storage, session_id, config = stock
    await storage.write(BUCKET, f"{session_id}/shot.png", PNG)
    assert await _read(storage, "shot.png", session_id, config) == PNG


@pytest.mark.asyncio
async def test_a_managed_conversation_reads_its_sandbox_folder_then_its_boundary(stock, tmp_path) -> None:
    storage, session_id, config = stock
    config = {**config, "workspace_boundary": "slack:c:C1"}
    boundary = "boundaries/slack:c:C1/workspace"
    (tmp_path / BUCKET / "sessions" / session_id / "a.png").write_bytes(PNG)
    await storage.write(BUCKET, f"{boundary}/a.png", OTHER)
    await storage.write(BUCKET, f"{boundary}/upload.png", OTHER)
    assert await _read(storage, "a.png", session_id, config) == PNG
    assert await _read(storage, "upload.png", session_id, config) == OTHER


@pytest.mark.asyncio
async def test_a_sub_agent_reads_its_roots_sandbox_folder(tmp_path) -> None:
    storage = LocalBackend(str(tmp_path))
    root, child = str(uuid.uuid4()), str(uuid.uuid4())
    workspace = storage.resolve_workspace_path(BUCKET, root)
    config = {"storage_bucket": BUCKET, "workspace_path": workspace, "sandbox_root_session_id": root}
    (tmp_path / BUCKET / "sessions" / root / "a.png").write_bytes(PNG)
    assert await _read(storage, "a.png", child, config) == PNG


@pytest.mark.asyncio
async def test_a_link_in_the_sandbox_folder_is_not_followed(stock, tmp_path) -> None:
    storage, session_id, config = stock
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    (tmp_path / BUCKET / "sessions" / session_id / "a.png").symlink_to(outside)
    with pytest.raises(ValueError, match="not found"):
        await _read(storage, "a.png", session_id, config)


@pytest.mark.asyncio
async def test_a_workspace_folder_that_is_a_link_is_not_followed(tmp_path) -> None:
    storage = LocalBackend(str(tmp_path))
    session_id, other = str(uuid.uuid4()), str(uuid.uuid4())
    theirs = storage.resolve_workspace_path(BUCKET, other)
    (tmp_path / BUCKET / "sessions" / other / "a.png").write_bytes(PNG)
    (tmp_path / BUCKET / "sessions" / session_id).symlink_to(theirs)
    config = {"storage_bucket": BUCKET, "workspace_path": str(tmp_path / BUCKET / "sessions" / session_id)}
    with pytest.raises(ValueError, match="not found"):
        await _read(storage, "a.png", session_id, config)


def test_a_workspace_outside_the_bucket_keeps_the_sessions_key(tmp_path) -> None:
    storage = LocalBackend(str(tmp_path / "base"))
    config = {"storage_bucket": BUCKET, "workspace_path": str(tmp_path / "elsewhere")}
    assert sandbox_read_keys(storage, BUCKET, config, "s1", "a.png") == ["s1/a.png"]


def test_object_storage_has_one_key() -> None:
    storage = S3Backend("http://s3.invalid")
    config = {"storage_bucket": BUCKET, "workspace_path": "/workspace"}
    assert sandbox_read_keys(storage, BUCKET, config, "s1", "a.png") == ["s1/a.png"]


def test_a_session_on_a_computer_keeps_its_key(tmp_path) -> None:
    storage = LocalBackend(str(tmp_path))
    workspace = storage.resolve_workspace_path(BUCKET, "s1")
    config = {
        "storage_bucket": BUCKET, "workspace_path": workspace,
        "execution": {"kind": "device", "device_id": str(uuid.uuid4())},
    }
    assert sandbox_read_keys(storage, BUCKET, config, "s1", "a.png") == ["s1/a.png"]


@pytest.mark.asyncio
async def test_the_image_cache_key_names_the_file_that_is_read(stock, tmp_path) -> None:
    storage, session_id, config = stock
    (tmp_path / BUCKET / "sessions" / session_id / "a.png").write_bytes(PNG)
    key = await _build_key("a.png", {
        "session_id": session_id, "storage": storage, "session_config": config,
        "workspace_path": config["workspace_path"],
    })
    assert key is not None and key[3] == f"sessions/{session_id}/a.png"


def test_without_a_no_follow_walk_the_sandbox_folder_is_not_read(stock, monkeypatch) -> None:
    storage, session_id, config = stock
    monkeypatch.setattr(local, "_BY_FOLDER", False)
    assert sandbox_read_keys(storage, BUCKET, config, session_id, "a.png") == [f"{session_id}/a.png"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file of any mode")
async def test_an_unreadable_file_in_the_sandbox_folder_does_not_hide_the_sessions_own(stock, tmp_path) -> None:
    storage, session_id, config = stock
    shown = tmp_path / BUCKET / "sessions" / session_id / "a.png"
    shown.write_bytes(OTHER)
    shown.chmod(0)
    await storage.write(BUCKET, f"{session_id}/a.png", PNG)
    try:
        assert await _read(storage, "a.png", session_id, config) == PNG
    finally:
        shown.chmod(0o644)


@pytest.mark.asyncio
async def test_no_cache_key_for_a_file_read_from_the_second_key(stock) -> None:
    storage, session_id, config = stock
    await storage.write(BUCKET, f"{session_id}/shot.png", PNG)
    key = await _build_key("shot.png", {
        "session_id": session_id, "storage": storage, "session_config": config,
        "workspace_path": config["workspace_path"],
    })
    assert key is None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [None, "slack:c:C1"])
async def test_a_fetched_channel_image_is_not_replaced_by_a_file_of_the_sandbox(stock, tmp_path, boundary) -> None:
    storage, session_id, config = stock
    if boundary:
        config = {**config, "workspace_boundary": boundary}
    path = "uploads/slack/fetch/F1-a.png"
    own = f"boundaries/{boundary}/workspace/{path}" if boundary else f"{session_id}/{path}"
    await storage.write(BUCKET, own, PNG)
    shown = tmp_path / BUCKET / "sessions" / session_id / path
    shown.parent.mkdir(parents=True)
    shown.write_bytes(OTHER)
    harness = SimpleNamespace(_storage=storage, _sandbox_pool=None)
    session = SimpleNamespace(id=session_id, channel="slack" if boundary else "web", config=config)
    assert await AgentHarness._read_workspace_image(harness, session, path) == PNG
