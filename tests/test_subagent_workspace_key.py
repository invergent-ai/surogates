"""A sub-agent's image tools reach the workspace its sandbox shows: its root's."""

from __future__ import annotations

import base64
import uuid

import pytest

from surogates.harness.image_read import _build_key
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import sandbox_workspace_key
from surogates.tools.builtin.browser import _save_screenshot_to_storage
from surogates.tools.builtin.media_gen import _save_media_bytes
from surogates.tools.builtin.vision import _image_ref_to_data_url

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
OTHER = b"\x89PNG\r\n\x1a\n" + b"1" * 16
BUCKET = "agent-bucket"


@pytest.fixture
def family(tmp_path):
    root, child = str(uuid.uuid4()), str(uuid.uuid4())
    storage = LocalBackend(str(tmp_path))
    config = {"storage_bucket": BUCKET, "sandbox_root_session_id": root}
    return storage, root, child, config


def test_a_child_key_is_under_its_root() -> None:
    assert sandbox_workspace_key({"sandbox_root_session_id": "root"}, "child", "a.png") == "root/a.png"


def test_a_session_with_no_root_keeps_its_own_key() -> None:
    assert sandbox_workspace_key({}, "own", "a.png") == "own/a.png"


def test_a_managed_child_stays_in_its_conversation() -> None:
    config = {"workspace_boundary": "slack:c:C1", "sandbox_root_session_id": "root", "storage_key_prefix": "p/a"}
    assert sandbox_workspace_key(config, "child", "a.png") == "p/a/boundaries/slack:c:C1/workspace/a.png"


@pytest.mark.asyncio
async def test_a_child_reads_the_image_its_sandbox_shows(family) -> None:
    storage, root, child, config = family
    await storage.write(BUCKET, f"{root}/a.png", PNG)
    await storage.write(BUCKET, f"{child}/a.png", OTHER)

    url, _ = await _image_ref_to_data_url(
        "a.png", workspace_path="/workspace", storage=storage, session_id=child, session_config=config,
    )

    assert base64.b64decode(url.split(",", 1)[1]) == PNG


@pytest.mark.asyncio
async def test_a_child_cache_key_names_the_file_it_reads(family) -> None:
    storage, root, child, config = family
    await storage.write(BUCKET, f"{root}/a.png", PNG)

    key = await _build_key(
        "a.png", {"session_id": child, "storage": storage, "session_config": config, "workspace_path": "/workspace"},
    )

    assert key is not None and key[3] == f"{root}/a.png"


@pytest.mark.asyncio
async def test_what_a_child_generates_lands_where_its_sandbox_shows_it(family) -> None:
    storage, root, child, config = family

    saved = await _save_media_bytes(
        PNG, relative_path="made.png", workspace_path=None, storage=storage, session_id=child, session_config=config,
    )

    assert saved is True
    assert await storage.read(BUCKET, f"{root}/made.png") == PNG
    assert not await storage.exists(BUCKET, f"{child}/made.png")


@pytest.mark.asyncio
async def test_a_child_screenshot_lands_where_its_sandbox_shows_it(family) -> None:
    storage, root, child, config = family

    saved = await _save_screenshot_to_storage(
        PNG, storage=storage, session_id=child, session_config=config, relative_path="shot.png",
    )

    assert saved == "shot.png"
    assert await storage.read(BUCKET, f"{root}/shot.png") == PNG
