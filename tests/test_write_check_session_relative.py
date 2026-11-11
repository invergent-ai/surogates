"""The write-protection check judges the file a write will reach.

``LocalWorkspaceIO.check_write`` (the deny list of credential and system
files, ``SUROGATES_WRITE_SAFE_ROOT``, the sensitive system paths) resolved a
relative path against the worker's own working folder, while the write
itself goes to the session's workspace.  It now resolves it as ``resolve()``
does: against the workspace.
"""

from __future__ import annotations

import json
import os

import pytest

from surogates.tools.builtin.file_ops import _patch_handler, _write_file_handler
from surogates.tools.workspace_io import local
from surogates.tools.workspace_io.local import LocalWorkspaceIO


@pytest.fixture
def places(tmp_path, monkeypatch):
    """A session workspace, and a worker folder the process runs in."""
    root = tmp_path / "sessions" / "s1"
    root.mkdir(parents=True)
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "etc").symlink_to("/etc")  # in the worker's folder only
    monkeypatch.chdir(worker)
    monkeypatch.delenv("SUROGATES_WRITE_SAFE_ROOT", raising=False)
    return root, worker


async def test_a_relative_path_is_judged_in_the_session_not_the_worker_folder(places):
    root, _ = places
    wio = LocalWorkspaceIO(str(root))
    # "etc/passwd" names the session's own etc/passwd, not the worker folder's link to /etc.
    assert await wio.check_write("etc/passwd") is None
    raw = await _write_file_handler({"path": "etc/passwd", "content": "x"}, workspace_path=str(root))
    assert "error" not in json.loads(raw), raw
    assert (root / "etc" / "passwd").read_text() == "x"


async def test_the_workers_home_is_not_where_a_relative_path_points(places, monkeypatch):
    root, _ = places
    monkeypatch.chdir(local._HOME)
    wio = LocalWorkspaceIO(str(root))
    # In the worker's home this would be its credentials; in the session it is a file of the session's.
    assert await wio.check_write(".ssh/authorized_keys") is None
    assert await wio.check_write(".bashrc") is None


_UP = "../" * 40  # past the filesystem root from any test folder


@pytest.mark.parametrize("path", [f"{_UP}etc/passwd", f"{_UP}etc/sudoers.d/x", f"a/{_UP}etc/shadow"])
async def test_a_relative_path_that_escapes_to_a_protected_file_is_refused(places, path):
    root, _ = places
    refusal = await LocalWorkspaceIO(str(root)).check_write(path)
    assert refusal is not None
    assert path in refusal and str(root) not in refusal  # names the path as written, not the host's


async def test_a_symlink_in_the_session_to_a_protected_file_is_refused(places):
    root, _ = places
    (root / "sys").symlink_to("/etc")
    refusal = await LocalWorkspaceIO(str(root)).check_write("sys/passwd")
    assert refusal is not None and "sys/passwd" in refusal
    (root / "keys").symlink_to(os.path.join(local._HOME, ".ssh"))
    assert await LocalWorkspaceIO(str(root)).check_write("keys/authorized_keys") is not None


async def test_the_safe_write_root_is_judged_from_the_session(places, monkeypatch):
    root, worker = places
    monkeypatch.setenv("SUROGATES_WRITE_SAFE_ROOT", str(root))
    wio = LocalWorkspaceIO(str(root))
    assert await wio.check_write("out/report.md") is None  # inside the safe root, wherever the worker runs
    assert await wio.check_write("/workspace/out/report.md") is None
    assert await wio.check_write("../s2/report.md") is not None  # outside it
    monkeypatch.setenv("SUROGATES_WRITE_SAFE_ROOT", str(worker))
    # The worker's folder is no longer where "out/report.md" goes, so it is outside the safe root.
    assert await wio.check_write("out/report.md") is not None


async def test_a_relative_escape_is_still_refused_by_the_workspace_check(places):
    root, _ = places
    raw = await _write_file_handler({"path": "../escape.txt", "content": "owned"}, workspace_path=str(root))
    assert "Path traversal blocked" in json.loads(raw)["error"]
    assert not (root.parent / "escape.txt").exists()


async def test_patch_uses_the_same_judgement(places):
    root, _ = places
    (root / "etc").mkdir()
    (root / "etc" / "notes.txt").write_text("old", encoding="utf-8")
    raw = await _patch_handler({"path": "etc/notes.txt", "old_string": "old", "new_string": "new"},
                               workspace_path=str(root))
    assert "error" not in json.loads(raw), raw
    assert (root / "etc" / "notes.txt").read_text(encoding="utf-8") == "new"


async def test_without_a_workspace_paths_are_judged_as_before(places):
    _, worker = places
    wio = LocalWorkspaceIO(None)
    # No workspace: the path is the worker's, as it always was.
    assert await wio.check_write("etc/passwd") is not None  # the worker folder's etc -> /etc
    assert await wio.check_write("/etc/passwd") is not None
    assert await wio.check_write(str(worker / "notes.txt")) is None


async def test_a_workspace_that_is_the_workers_home_still_refuses_its_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # the worker runs elsewhere
    wio = LocalWorkspaceIO(local._HOME)
    # Judged in the worker's folder, the old check let these through; they land in the home's .ssh.
    assert await wio.check_write(".ssh/authorized_keys") is not None
    assert await wio.check_write("x/../.netrc") is not None
    assert await wio.check_write("~/.ssh/config") is not None


async def test_the_check_judges_resolves_file_for_every_spelling(places, tmp_path):
    root, _ = places
    (root / "sys").symlink_to("/etc")
    link = tmp_path / "ws-link"
    link.symlink_to(root)
    for wio in (LocalWorkspaceIO(str(root)), LocalWorkspaceIO(str(link)), LocalWorkspaceIO(str(root) + "/")):
        assert await wio.check_write("/workspace/sys/passwd") is not None
        assert await wio.check_write("sys/../notes.md") is None
        for path in ("notes.md", "/workspace/out/a.md"):
            assert await wio.check_write(path) is None
            assert (await wio.resolve(path)).startswith(str(root.resolve()))


async def test_a_traversal_refusal_names_no_host_path(places):
    root, _ = places
    raw = await _write_file_handler({"path": "../escape.txt", "content": "x"}, workspace_path=str(root))
    error = json.loads(raw)["error"]
    assert "../escape.txt" in error and str(root) not in error and str(root.parent) not in error


async def test_a_link_planted_at_the_temporary_name_is_never_written_through(places, tmp_path):
    root, _ = places
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    (root / "note.txt.tmp").symlink_to(outside)
    raw = await _write_file_handler({"path": "note.txt", "content": "PWNED"}, workspace_path=str(root))
    assert "error" not in json.loads(raw), raw
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert not (root / "note.txt").is_symlink()
    assert (root / "note.txt").read_text(encoding="utf-8") == "PWNED"
    assert not (root / "note.txt.tmp").exists()


async def test_a_new_file_keeps_the_usual_mode(places):
    root, _ = places
    await LocalWorkspaceIO(str(root)).write(str(root / "plain.txt"), b"x")
    umask = os.umask(0)
    os.umask(umask)
    assert (root / "plain.txt").stat().st_mode & 0o777 == 0o666 & ~umask


async def test_an_overwritten_file_keeps_its_mode(places):
    root, _ = places
    target = root / "script.sh"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o750)
    await LocalWorkspaceIO(str(root)).write(str(target), b"new")
    assert target.stat().st_mode & 0o777 == 0o750
    assert target.read_text(encoding="utf-8") == "new"
