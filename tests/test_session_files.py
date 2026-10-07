"""session_files: a cloud session's files are its workspace in object storage, under its workspace's prefix."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.session.files import DeviceAccess, session_files
from surogates.storage.backend import LocalBackend

pytestmark = pytest.mark.asyncio

BUCKET = "agent-bucket"


def cloud(**config) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), parent_id=None, channel="web", config={"storage_bucket": BUCKET, **config})


async def written_under(session, tmp_path) -> list[str]:
    storage = LocalBackend(str(tmp_path))
    await storage.create_bucket(BUCKET)
    async with session_files(session, storage=storage, session_factory=None, redis=None) as files:
        await files.write(await files.resolve("notes/a.md"), b"a")
    return await storage.list_keys(BUCKET)


async def test_a_root_reads_and_writes_its_own_prefix(tmp_path):
    session = cloud()
    assert await written_under(session, tmp_path) == [f"{session.id}/notes/a.md"]


async def test_a_session_created_under_another_works_in_its_roots_prefix(tmp_path):
    root = uuid4()
    assert await written_under(cloud(sandbox_root_session_id=str(root)), tmp_path) == [f"{root}/notes/a.md"]


async def test_an_older_child_with_no_recorded_root_keeps_its_own_prefix(tmp_path):
    # Unlike sandbox_session_key, which falls back to the parent.
    session = cloud()
    session.parent_id = uuid4()
    assert await written_under(session, tmp_path) == [f"{session.id}/notes/a.md"]


async def test_a_managed_channels_session_works_in_its_conversations_workspace(tmp_path):
    session = cloud(workspace_boundary="slack:c:G1")
    session.channel = "slack"
    assert await written_under(session, tmp_path) == ["boundaries/slack:c:G1/workspace/notes/a.md"]


async def test_the_agents_key_prefix_comes_first(tmp_path):
    session = cloud(storage_key_prefix="agents/a1")
    assert await written_under(session, tmp_path) == [f"agents/a1/{session.id}/notes/a.md"]


async def test_a_change_comes_only_with_its_callers_access_to_that_session(tmp_path):
    session = cloud()
    # None; another session's; and one that did not check the chat's folder is bound.
    for access in (None, DeviceAccess(uuid4(), bound=True), DeviceAccess(session.id, bound=False)):
        with pytest.raises(RuntimeError, match="require_device_access"):
            async with session_files(
                session, storage=LocalBackend(str(tmp_path)), session_factory=None, redis=None,
                change="upload a.txt", access=access,
            ):
                pass
    async with session_files(
        session, storage=LocalBackend(str(tmp_path)), session_factory=None, redis=None,
        change="upload a.txt", access=DeviceAccess(session.id, bound=True),
    ):
        pass
