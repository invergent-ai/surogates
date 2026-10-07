"""StorageWorkspaceIO: a cloud workspace's files in object storage, under its prefix."""

from __future__ import annotations

import pytest

from surogates.storage.backend import LocalBackend
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import StorageWorkspaceIO, Walk

pytestmark = pytest.mark.asyncio

BUCKET = "agent-bucket"
PREFIX = "root-1/"


@pytest.fixture
async def storage(tmp_path) -> LocalBackend:
    backend = LocalBackend(str(tmp_path))
    await backend.create_bucket(BUCKET)
    return backend


@pytest.fixture
def files(storage) -> StorageWorkspaceIO:
    return StorageWorkspaceIO(storage, bucket=BUCKET, prefix=PREFIX)


async def test_keys_are_paths_from_the_workspace_root(files):
    assert await files.resolve("notes/a.md") == "notes/a.md"
    # As given, as the routes used it: an object stored under it keeps its name.
    assert await files.resolve("./notes//a.md") == "./notes//a.md"
    assert await files.resolve(".") == ""
    assert await files.resolve("") == ""
    for path in ("../other/a.md", "notes/../../a.md", "/etc/passwd"):
        with pytest.raises(WorkspaceSandboxError):
            await files.resolve(path)


async def test_files_live_under_the_prefix(files, storage):
    await files.write("notes/a.md", b"alpha")
    assert await storage.read(BUCKET, f"{PREFIX}notes/a.md") == b"alpha"
    assert await files.read("notes/a.md") == b"alpha"
    assert await files.read("notes/a.md", max_bytes=2) == b"al"
    st = await files.stat("notes/a.md")
    assert (st.is_dir, st.size) == (False, 5)
    await files.delete("notes/a.md")
    assert not await storage.exists(BUCKET, f"{PREFIX}notes/a.md")


async def test_a_missing_file_is_not_found(files):
    assert await files.stat("missing.md") is None
    with pytest.raises(FileNotFoundError):
        await files.read("missing.md")
    with pytest.raises(FileNotFoundError):
        await files.delete("missing.md")


async def test_a_walk_lists_the_files_under_a_folder_of_this_workspace(files, storage):
    await files.write("a.txt", b"alpha")
    await files.write("sub/b.md", b"b")
    await files.write("node_modules/x/i.js", b"")
    await storage.write(BUCKET, "root-2/elsewhere.txt", b"not this workspace's")
    # One listing holds them all: what a caller shows is its to filter, as the file panel always has.
    assert await files.walk("", skip={"node_modules"}) == Walk(
        [("a.txt", 5), ("node_modules/x/i.js", 0), ("sub/b.md", 1)], False, None,
    )
    assert await files.walk("sub", skip=()) == Walk([("b.md", 1)], False, None)


class _Unreadable(LocalBackend):
    """Object storage that cannot say what a key holds, as S3 answering 403 or throttling: its exists swallows it."""

    async def exists(self, bucket: str, key: str) -> bool:
        return False

    async def stat(self, bucket: str, key: str) -> dict:
        raise RuntimeError("An error occurred (403) when calling the HeadObject operation: Forbidden")


async def test_a_file_storage_cannot_say_is_not_found_as_before(tmp_path):
    # The download and a message's attachments asked exists() first, which swallows every error: so they still answer
    # 404 and 422, not 500.
    files = StorageWorkspaceIO(_Unreadable(str(tmp_path)), bucket=BUCKET, prefix=PREFIX)
    assert await files.stat("notes/a.md") is None
