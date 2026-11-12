"""A folder swapped for a link between the path check and a read cannot make the worker read outside the workspace.

The read side of #651: ``resolve()`` checks a path and ``read``, ``read_lines``, ``stat``, ``list_dir``,
``local_file`` and ``ripgrep`` used it later by path.  A command still running in the workspace could swap a folder
on it for a link to somewhere else in between, and the worker (which srt does not confine) read what the link
pointed at: files srt denies to commands, another session's workspace.  They now reach the file or folder from the
workspace one folder at a time, with no link followed.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from surogates.tools.workspace_io import local as local_module
from surogates.tools.workspace_io.local import LocalWorkspaceIO, _paths_back

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")


@pytest.fixture
def places(tmp_path):
    ws, out = tmp_path / "ws", tmp_path / "outside"
    (ws / "a" / "b").mkdir(parents=True)
    (ws / "a" / "b" / "notes.txt").write_text("inside\n")
    (out / "b").mkdir(parents=True)
    (out / "b" / "notes.txt").write_text("SECRET\n")
    (out / "b" / "secret.txt").write_text("SECRET\n")
    return ws, out


def _swap(ws: Path, out: Path) -> None:
    """What a command in the workspace can do at any moment: put a link where a folder was."""
    (ws / "a").rename(ws / "a.old")
    (ws / "a").symlink_to(out, target_is_directory=True)


async def test_a_read_through_a_swapped_folder_is_refused(places):
    ws, out = places
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("a/b/notes.txt")
    _swap(ws, out)
    with pytest.raises(PermissionError, match="replaced after the path was checked") as refused:
        await wio.read(key)
    assert str(ws.parent) not in str(refused.value)
    with pytest.raises(PermissionError):
        await wio.read_lines(key, encoding="utf-8", offset=1, limit=10, max_bytes=1000)


async def test_stat_and_list_dir_through_a_swapped_folder_see_nothing_outside(places):
    ws, out = places
    wio = LocalWorkspaceIO(str(ws))
    key, folder = await wio.resolve("a/b/secret.txt"), await wio.resolve("a/b")
    _swap(ws, out)
    assert await wio.stat(key) is None
    with pytest.raises(PermissionError):
        await wio.list_dir(folder)


async def test_a_file_name_swapped_for_a_link_is_not_read(places):
    ws, out = places
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("a/b/notes.txt")
    (ws / "a" / "b" / "notes.txt").unlink()
    (ws / "a" / "b" / "notes.txt").symlink_to(out / "b" / "secret.txt")
    with pytest.raises(PermissionError):
        await wio.read(key)
    assert await wio.stat(key) is None


@pytest.mark.skipif(not local_module._PROC_FDS, reason="needs /proc")
async def test_a_parser_reads_the_file_the_walk_reached_under_its_own_name(places):
    ws, out = places
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("a/b/notes.txt")
    async with wio.local_file(key) as local:
        assert local.name == "notes.txt" and local.suffix == ".txt"
        _swap(ws, out)  # swapped while the parser has the path: it still reads the workspace's file
        assert local.read_text() == "inside\n"
    assert not local.exists()
    with pytest.raises(PermissionError):
        async with wio.local_file(key):
            pass


async def test_reads_still_work(places):
    ws, _ = places
    (ws / "shortcut").symlink_to(ws / "a" / "b", target_is_directory=True)
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("a/b/notes.txt")
    assert await wio.read(key) == b"inside\n"
    assert await wio.read(key, max_bytes=3) == b"ins"
    page = await wio.read_lines(key, encoding="utf-8", offset=1, limit=10, max_bytes=1000)
    assert page.data == b"inside\n"
    st = await wio.stat(key)
    assert st is not None and not st.is_dir and st.size == 7
    assert (await wio.stat(str(ws))).is_dir
    assert sorted(await wio.list_dir(str(ws))) == ["a", "shortcut"]
    assert await wio.list_dir(await wio.resolve("shortcut")) == ["notes.txt"]  # resolve() followed it
    assert await wio.stat(await wio.resolve("a/missing.txt")) is None
    with pytest.raises(FileNotFoundError):
        await wio.read(await wio.resolve("a/missing.txt"))
    with pytest.raises(IsADirectoryError):
        await wio.read(await wio.resolve("a"))
    with pytest.raises(NotADirectoryError):
        await wio.list_dir(key)


def test_ripgreps_paths_are_written_back_as_keys():
    assert _paths_back("./sub/a.txt\n./b.md\n", "./", "/ws/", "files") == "/ws/sub/a.txt\n/ws/b.md\n"
    assert _paths_back("./sub/a.txt:3\n", "./", "/ws/", "count") == "/ws/sub/a.txt:3\n"
    line = json.dumps({"type": "match", "data": {"path": {"text": "./sub/a.txt"}, "lines": {"text": "./x\n"}}},
                      separators=(",", ":"))
    back = json.loads(_paths_back(line, "./", "/ws/", "json"))
    assert back["data"]["path"]["text"] == "/ws/sub/a.txt" and back["data"]["lines"]["text"] == "./x\n"
    assert _paths_back('{"data":{"path":{"text":"/proc/9/fd/5"}}}', "/proc/9/fd/5", "/ws/a.txt", "json") == \
        '{"data":{"path":{"text":"/ws/a.txt"}}}'


@pytest.mark.skipif(not local_module._PROC_FDS, reason="needs /proc")
async def test_ripgrep_runs_in_the_folder_the_walk_reached(places, monkeypatch):
    """ripgrep takes a path: it is started in the folder the walk opened, whatever the path names by then."""
    ws, out = places
    wio = LocalWorkspaceIO(str(ws))
    folder = await wio.resolve("a")
    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"./b/notes.txt\n", b""

    async def exec_(program, *args, cwd=None, **_kwargs):
        seen["cwd"] = os.path.realpath(cwd) if cwd else None
        seen["target"] = args[-1]
        seen["inode"] = os.stat(cwd).st_ino if cwd else None
        return _Proc()

    monkeypatch.setattr(local_module, "_RIPGREP_PATH", "rg")
    monkeypatch.setattr(local_module, "_bwrap_works", False)  # the unconfined path: started in the folder
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    inode = os.stat(ws / "a").st_ino
    out_text = await wio.ripgrep(folder, mode="files", pattern="*.txt")
    assert seen["target"] == "." and seen["inode"] == inode
    assert out_text == f"{folder}/b/notes.txt\n"
    _swap(ws, out)
    with pytest.raises(PermissionError):
        await wio.ripgrep(folder, mode="files", pattern="*.txt")


def _fake_rg(tmp_path: Path) -> str:
    """A stand-in for ripgrep (there is none on the test box) that does what a walk by path does: it reads a file
    through a folder that is a link to outside the workspace by the time it gets there."""
    rg = tmp_path / "rg"
    rg.write_text("#!/bin/sh\ncat ./sub/secret.txt 2>/dev/null\nfind . -name '*.txt' -type f | sort\n")
    rg.chmod(0o755)
    return str(rg)


@pytest.mark.skipif(not local_module._PROC_FDS or local_module._BWRAP is None, reason="needs /proc and bubblewrap")
async def test_ripgreps_own_walk_is_confined_to_the_folder_it_searches(places, tmp_path, monkeypatch):
    ws, out = places
    (ws / "a" / "sub").symlink_to(out / "b", target_is_directory=True)  # swapped in during ripgrep's own walk
    wio = LocalWorkspaceIO(str(ws))
    folder = await wio.resolve("a")
    monkeypatch.setattr(local_module, "_RIPGREP_PATH", _fake_rg(tmp_path))
    monkeypatch.setattr(local_module, "_bwrap_works", None)
    if not await local_module._bwrap_can_confine(local_module._RIPGREP_PATH):
        pytest.skip("bubblewrap cannot run here")
    found = await wio.ripgrep(folder, mode="files", pattern="*.txt")
    assert "SECRET" not in found
    assert found.splitlines() == [f"{folder}/b/notes.txt"]
    # Without the confinement, the same walk reads through the link.
    monkeypatch.setattr(local_module, "_bwrap_works", False)
    assert "SECRET" in await wio.ripgrep(folder, mode="files", pattern="*.txt")


async def test_a_folder_that_reappears_during_the_check_counts_as_swapped(places, monkeypatch):
    """The open saw a link, and by the check the folder is back: still refused, never "not a folder"."""
    ws, out = places
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("a/b/notes.txt")
    _swap(ws, out)
    real_stat = os.stat

    def stat(path, *args, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None and path == "a" and not follow_symlinks:
            (ws / "a").unlink()
            (ws / "a.old").rename(ws / "a")  # swapped back just before the check
        return real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", stat)
    with pytest.raises(PermissionError, match="replaced after the path was checked"):
        await wio.read(key)


async def test_a_fifo_or_a_folder_under_a_files_name_is_not_waited_on(places):
    ws, _ = places
    os.mkfifo(ws / "a" / "b" / "pipe.pdf")
    wio = LocalWorkspaceIO(str(ws))
    key = await wio.resolve("a/b/pipe.pdf")
    # In a daemon thread, so a blocking open would fail the test by its timeout instead of hanging it.
    import threading

    result: dict[str, BaseException] = {}

    def read() -> None:
        try:
            asyncio.run(wio.read(key))
        except BaseException as exc:  # noqa: BLE001 -- what the read raised is the result
            result["error"] = exc

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(5)
    assert not reader.is_alive(), "the read waited on the FIFO"
    assert isinstance(result.get("error"), OSError) and "Not a regular file" in str(result["error"])
    with pytest.raises(OSError):
        async with wio.local_file(await wio.resolve("a/b/pipe.pdf")):
            pass
    with pytest.raises(IsADirectoryError):
        async with wio.local_file(await wio.resolve("a/b")):
            pass


async def test_every_first_search_waits_for_the_one_bubblewrap_answer(monkeypatch):
    """Searches that start while bubblewrap is being asked are confined too: none runs unconfined meanwhile."""
    import time

    calls = []

    class _Done:
        returncode = 0

    def run(*_args, **_kwargs):
        calls.append(1)
        time.sleep(0.2)
        return _Done()

    monkeypatch.setattr(local_module, "_BWRAP", "/usr/bin/bwrap")
    monkeypatch.setattr(local_module, "_bwrap_works", None)
    monkeypatch.setattr(local_module.subprocess, "run", run)
    answers = await asyncio.gather(*(local_module._bwrap_can_confine("/usr/bin/rg") for _ in range(4)))
    assert answers == [True] * 4 and calls == [1]


async def test_a_bubblewrap_that_does_not_answer_is_asked_again(monkeypatch):
    import subprocess

    def slow(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("bwrap", 10)

    monkeypatch.setattr(local_module, "_BWRAP", "/usr/bin/bwrap")
    monkeypatch.setattr(local_module, "_bwrap_works", None)
    monkeypatch.setattr(local_module.subprocess, "run", slow)
    # This search is confined anyway (a bubblewrap that cannot run fails it); the next one asks again.
    assert await local_module._bwrap_can_confine("/usr/bin/rg")
    assert local_module._bwrap_works is None
