"""A file changed on the user's computer between a tool's read and its write: the change stays, and the model is told.

Each tool writes on the revision its own stat gave, in the same call.  The cloud does not check it, so a cloud
sandbox writes as it always has.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.harness.tool_exec import should_parallelize
from surogates.tools.builtin import file_ops
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import CONFLICT, InProcessRunner


@pytest.fixture(autouse=True)
def _fresh_tracker():
    file_ops._read_tracker.clear()
    yield
    file_ops._read_tracker.clear()


@pytest.fixture
def folder(tmp_path) -> Path:
    root = (tmp_path / "laptop").resolve()
    root.mkdir()
    (root / "a.py").write_text("one\ntwo\n")
    return root


# What the user saves meanwhile.  Another size than the file's, so the revision moves even within the
# filesystem's timestamp tick.
SAVED = b"one\ntwo\nthree\n"


class Saving:
    """The reference laptop, where the user saves *path* just before the first write to it runs.

    With *keep_mtime*, the save puts the file's mtime back, as ``cp -p`` or ``rsync -t`` does.
    """

    def __init__(self, folder: Path, path: Path, *, keep_mtime: bool = False) -> None:
        self.inner = InProcessRunner(LocalWorkspaceIO(str(folder)))
        self.path = path
        self.keep_mtime = keep_mtime
        self.saved = False

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
        if kind == "write" and args["key"] == str(self.path) and not self.saved:
            self.saved = True
            st = self.path.stat()
            self.path.write_bytes(SAVED)
            if self.keep_mtime:
                os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        return await self.inner.run(kind, args, payload)


class SavingLocally(LocalWorkspaceIO):
    """A cloud workspace, where something saves *path* just before a write to it."""

    def __init__(self, folder: Path, path: Path) -> None:
        super().__init__(str(folder))
        self.path = path

    async def write(self, key: str, data: bytes, *, expected_revision: str | None = None) -> None:
        if key == str(self.path):
            self.path.write_bytes(SAVED)
        await super().write(key, data, expected_revision=expected_revision)


class HeldWrites:
    """The reference laptop, running writes only once *count* of them wait: calls that ran side by side."""

    def __init__(self, folder: Path, count: int) -> None:
        self.inner = InProcessRunner(LocalWorkspaceIO(str(folder)))
        self.count = count
        self.waiting = 0
        self.all_there = asyncio.Event()

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
        if kind == "write":
            self.waiting += 1
            if self.waiting == self.count:
                self.all_there.set()
            await self.all_there.wait()
        return await self.inner.run(kind, args, payload)


class SavingThenBreaking(Saving):
    """The reference laptop, where the user saves *path*, and then the next stat is answered with nonsense."""

    def __init__(self, folder: Path, path: Path) -> None:
        super().__init__(folder, path, keep_mtime=True)
        self.broke = False

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
        if kind == "stat" and self.saved and not self.broke:
            self.broke = True
            return {"ok": "nonsense"}
        return await super().run(kind, args, payload)


class ConflictOnResolve:
    """A computer that answers ``resolve`` with a conflict, which only a write should get."""

    def __init__(self, folder: Path) -> None:
        self.inner = InProcessRunner(LocalWorkspaceIO(str(folder)))

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
        if kind == "resolve":
            return {"error": {"type": "conflict", "message": "not a write"}}
        return await self.inner.run(kind, args, payload)


def on_device(runner) -> dict:
    return {"workspace_io": DeviceWorkspaceIO(runner, root=str(runner.inner.folder.root))}


async def patch(kwargs: dict, **arguments) -> dict:
    return json.loads(await file_ops._patch_handler(arguments, **kwargs))


async def write_file(kwargs: dict, **arguments) -> dict:
    return json.loads(await file_ops._write_file_handler(arguments, **kwargs))


async def conflicting(kwargs: dict, call: str) -> dict:
    """Edit a.py with *call*, whose write the user's save makes conflict."""
    if call == "replace":
        return await patch(kwargs, path="a.py", old_string="two", new_string="2")
    if call == "v4a":
        return await patch(kwargs, mode="patch", patch="*** Begin Patch\n*** Update File: a.py\n-two\n+2\n*** End Patch")
    return await write_file(kwargs, path="a.py", content="mine\n")


async def test_a_patch_keeps_what_the_user_saved_after_it_read_the_file(folder):
    runner = Saving(folder, folder / "a.py")
    result = await patch(on_device(runner), path="a.py", old_string="two", new_string="2")
    assert result == {"error": f"Failed to write patched file: {CONFLICT.format(folder / 'a.py')}"}
    assert (folder / "a.py").read_bytes() == SAVED
    assert runner.inner.kinds.count("write") == 1


async def test_a_cloud_patch_writes_as_it_always_has(folder):
    result = await patch(
        {"workspace_io": SavingLocally(folder, folder / "a.py")}, path="a.py", old_string="two", new_string="2",
    )
    assert result["status"] == "ok"
    assert (folder / "a.py").read_text() == "one\n2\n"


async def test_a_v4a_patch_reports_the_file_that_changed_and_writes_the_others(folder):
    runner = Saving(folder, folder / "a.py")
    result = await patch(on_device(runner), mode="patch", patch=(
        "*** Begin Patch\n*** Add File: b.py\n+bee\n*** Update File: a.py\n-two\n+2\n*** End Patch"
    ))
    failed = f"Failed to write: {CONFLICT.format(folder / 'a.py')}"
    assert result["status"] == "partial"
    assert result["files"] == [
        {"path": "b.py", "operation": "created", "status": "ok", "bytes_written": 3},
        {"path": "a.py", "error": failed},
    ]
    assert result["errors"] == [failed]
    assert (folder / "a.py").read_bytes() == SAVED
    assert (folder / "b.py").read_text() == "bee"


async def test_a_v4a_patch_neither_lints_nor_marks_read_a_file_it_did_not_write(folder):
    kwargs = on_device(Saving(folder, folder / "a.py"))
    await file_ops._read_file_handler({"path": "a.py"}, **kwargs)
    assert str(folder / "a.py") in file_ops._read_tracker["default"]["read_timestamps"]
    result = await patch(kwargs, mode="patch", patch=(
        "*** Begin Patch\n*** Add File: b.py\n+bee\n*** Update File: a.py\n-two\n+2\n*** End Patch"
    ))
    assert result["status"] == "partial"
    # In Ask every time, linting a.py would ask the user about a file the patch did not change.
    assert set(result["lint"]) == {"b.py"}
    # Its read is forgotten, not refreshed: whatever the mtimes, a.py no longer counts as read.
    assert str(folder / "a.py") not in file_ops._read_tracker["default"]["read_timestamps"]
    assert str(folder / "b.py") in file_ops._read_tracker["default"]["read_timestamps"]


async def test_a_v4a_patch_writes_nothing_more_to_a_file_whose_write_conflicted(folder):
    runner = Saving(folder, folder / "a.py")
    result = await patch(on_device(runner), mode="patch", patch=(
        "*** Begin Patch\n*** Update File: a.py\n-two\n+2\n*** Update File: ./a.py\n-one\n+1\n*** End Patch"
    ))
    # Its second block would land on the user's save without the first.
    failed = f"Failed to write: {CONFLICT.format(folder / 'a.py')}"
    assert result["files"] == [{"path": "a.py", "error": failed}, {"path": "./a.py", "error": failed}]
    assert (folder / "a.py").read_bytes() == SAVED
    assert runner.inner.kinds.count("write") == 1


@pytest.mark.parametrize("block", ["*** Delete File: a.py\n-one\n", "*** Add File: a.py\n+new\n"])
async def test_a_v4a_patch_neither_deletes_nor_replaces_a_file_whose_write_conflicted(folder, block):
    runner = Saving(folder, folder / "a.py")
    result = await patch(on_device(runner), mode="patch", patch=(
        f"*** Begin Patch\n*** Update File: a.py\n-two\n+2\n{block}*** End Patch"
    ))
    failed = f"Failed to write: {CONFLICT.format(folder / 'a.py')}"
    assert result["files"] == [{"path": "a.py", "error": failed}, {"path": "a.py", "error": failed}]
    assert (folder / "a.py").read_bytes() == SAVED


@pytest.mark.parametrize("call", ["replace", "v4a", "write_file"])
async def test_the_read_a_conflict_asks_for_shows_the_file_even_with_its_mtime_put_back(folder, call):
    kwargs = on_device(Saving(folder, folder / "a.py", keep_mtime=True))
    assert json.loads(await file_ops._read_file_handler({"path": "a.py"}, **kwargs))["content"] == "one\ntwo\n"
    result = await conflicting(kwargs, call)
    assert CONFLICT.format(folder / "a.py") in json.dumps(result)
    # Not "unchanged since last read": the mtime is the same, the file is not.
    again = json.loads(await file_ops._read_file_handler({"path": "a.py"}, **kwargs))
    assert again["content"] == SAVED.decode()


@pytest.mark.parametrize("call", ["replace", "v4a", "write_file"])
async def test_a_write_file_sent_after_a_conflict_without_a_read_is_refused(folder, call):
    kwargs = on_device(Saving(folder, folder / "a.py"))
    assert "content" in json.loads(await file_ops._read_file_handler({"path": "a.py"}, **kwargs))
    assert CONFLICT.format(folder / "a.py") in json.dumps(await conflicting(kwargs, call))
    # The model skipped the read the conflict asked for: the user's save is not written over.
    result = await write_file(kwargs, path="a.py", content="mine\n")
    assert "has not been read in this session" in result["error"]
    assert (folder / "a.py").read_bytes() == SAVED


async def test_a_conflict_forgets_the_read_even_when_the_patch_then_fails(folder):
    (folder / "b.py").write_text("bee\n")
    kwargs = on_device(SavingThenBreaking(folder, folder / "a.py"))
    assert json.loads(await file_ops._read_file_handler({"path": "a.py"}, **kwargs))["content"] == "one\ntwo\n"
    result = await patch(kwargs, mode="patch", patch=(
        "*** Begin Patch\n*** Update File: a.py\n-two\n+2\n*** Update File: b.py\n-bee\n+B\n*** End Patch"
    ))
    assert result == {"error": "The computer returned an invalid stat"}
    # a.py's mtime is put back, and still the read shows the user's save.
    again = json.loads(await file_ops._read_file_handler({"path": "a.py"}, **kwargs))
    assert again["content"] == SAVED.decode()


async def test_write_file_reports_a_conflict_on_resolve_as_an_error(folder, caplog):
    with caplog.at_level(logging.ERROR, logger=file_ops.logger.name):
        result = await write_file(on_device(ConflictOnResolve(folder)), path="a.py", content="mine\n")
    assert result == {"error": "not a write"}
    assert caplog.records == []


async def test_write_file_keeps_what_the_user_saved_during_its_call(folder, caplog):
    runner = Saving(folder, folder / "a.py")
    kwargs = on_device(runner)
    assert "content" in json.loads(await file_ops._read_file_handler({"path": "a.py"}, **kwargs))
    with caplog.at_level(logging.ERROR, logger=file_ops.logger.name):
        result = json.loads(await file_ops._write_file_handler({"path": "a.py", "content": "mine\n"}, **kwargs))
    assert result == {"error": CONFLICT.format(folder / "a.py")}
    assert (folder / "a.py").read_bytes() == SAVED
    # Expected, as a denied write is: no traceback in the worker's log.
    assert caplog.records == []


async def test_two_patches_through_two_spellings_of_one_path_conflict_once(folder):
    calls = [
        {"function": {"name": "patch", "arguments": json.dumps({"path": path, "old_string": old, "new_string": new})}}
        for path, old, new in [("./a.py", "one", "1"), ("a.py", "two", "2")]
    ]
    # The batch runs them side by side: its overlap check compares the paths as they are written.
    assert should_parallelize(calls)
    kwargs = on_device(HeldWrites(folder, 2))
    # Bounded: a patch that failed before its write would leave the other waiting for ever.
    results = await asyncio.wait_for(
        asyncio.gather(*(patch(kwargs, **json.loads(c["function"]["arguments"])) for c in calls)), 10.0,
    )
    landed = [result for result in results if result.get("status") == "ok"]
    assert len(landed) == 1
    assert [result for result in results if "error" in result] == [
        {"error": f"Failed to write patched file: {CONFLICT.format(folder / 'a.py')}"},
    ]
    # Exactly one edit is in the file, and the other was never written over it.
    assert (folder / "a.py").read_text() in {"1\ntwo\n", "one\n2\n"}
