"""A folder swapped for a link between the path check and the write cannot carry the write out of the workspace.

``resolve()`` checks a path (symlinks resolved, then kept inside the workspace), and ``write()`` / ``delete()`` act
on it later.  A command still running in the workspace could swap a folder on that path for a link to somewhere
else in between: the write then landed outside the workspace, and a delete removed a file there.  Now the file's
folder is reached from the workspace one folder at a time with no link followed, and the file is written and
renamed (or removed) inside that folder.
"""

from __future__ import annotations

import os
import stat

import pytest

from surogates.tools.workspace_io.local import LocalWorkspaceIO

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")


@pytest.fixture
def places(tmp_path):
    ws, out = tmp_path / "ws", tmp_path / "outside"
    (ws / "a" / "b").mkdir(parents=True)
    out.mkdir()
    return ws, out


def _swap_for_link(folder, target):
    """What a command in the workspace can do at any moment: put a link where a folder was."""
    folder.rename(folder.with_name(folder.name + ".old"))
    folder.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("swapped", ["a", "a/b"])
async def test_a_folder_swapped_after_the_check_does_not_carry_the_write_out(places, swapped):
    ws, out = places
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("a/b/report.txt")
    _swap_for_link(ws / swapped, out)
    with pytest.raises(PermissionError, match="replaced after the path was checked"):
        await wio.write(key, b"secret")
    assert list(out.iterdir()) == []


async def test_a_folder_swapped_after_the_check_does_not_carry_a_delete_out(places):
    ws, out = places
    (ws / "a" / "b" / "victim.txt").write_text("mine")
    (out / "victim.txt").write_text("not the workspace's")
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("a/b/victim.txt")
    _swap_for_link(ws / "a" / "b", out)
    with pytest.raises(PermissionError, match="replaced after the path was checked"):
        await wio.delete(key)
    assert (out / "victim.txt").read_text() == "not the workspace's"


async def test_a_write_still_makes_its_folders_keeps_the_mode_and_replaces(places):
    ws, _ = places
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("new/deeper/notes.md")
    await wio.write(key, b"one")
    assert (ws / "new" / "deeper" / "notes.md").read_bytes() == b"one"
    os.chmod(key, 0o640)
    await wio.write(key, b"two")
    assert (ws / "new" / "deeper" / "notes.md").read_bytes() == b"two"
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o640
    assert sorted(p.name for p in (ws / "new" / "deeper").iterdir()) == ["notes.md"]
    await wio.delete(key)
    assert not (ws / "new" / "deeper" / "notes.md").exists()


async def test_a_link_inside_the_workspace_resolved_at_the_check_is_still_written_through(places):
    """resolve() follows a link that stays inside the workspace; the write goes to the folder it names."""
    ws, _ = places
    (ws / "shortcut").symlink_to(ws / "a" / "b", target_is_directory=True)
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("shortcut/via-link.txt")
    await wio.write(key, b"ok")
    assert (ws / "a" / "b" / "via-link.txt").read_bytes() == b"ok"


async def test_a_planted_temporary_link_is_still_not_followed(places):
    ws, out = places
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("a/b/file.txt")
    (ws / "a" / "b" / "file.txt.tmp").symlink_to(out / "stolen")
    await wio.write(key, b"data")
    assert (ws / "a" / "b" / "file.txt").read_bytes() == b"data"
    assert not (out / "stolen").exists()


async def test_a_workspace_reached_through_a_link_is_written_as_before(tmp_path):
    real = tmp_path / "real-ws"
    real.mkdir()
    (tmp_path / "ws-link").symlink_to(real, target_is_directory=True)
    wio = LocalWorkspaceIO(str(tmp_path / "ws-link"))
    key = await wio.resolve("x/y.txt")
    await wio.write(key, b"z")
    assert (real / "x" / "y.txt").read_bytes() == b"z"


async def test_without_a_workspace_a_write_goes_by_path_as_before(tmp_path):
    wio = LocalWorkspaceIO(None)
    key = await wio.resolve(str(tmp_path / "free" / "f.txt"))
    await wio.write(key, b"f")
    assert (tmp_path / "free" / "f.txt").read_bytes() == b"f"
