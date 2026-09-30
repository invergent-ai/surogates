"""LocalWorkspaceIO: this host's filesystem and shell behind WorkspaceIO."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import FileStat, LocalWorkspaceIO, workspace_io_from


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def wio(root) -> LocalWorkspaceIO:
    return LocalWorkspaceIO(workspace_path=str(root))


async def test_resolve_starts_relative_paths_at_the_root(wio, root):
    assert await wio.resolve("a/b.txt") == str(root / "a" / "b.txt")


async def test_resolve_refuses_paths_outside_the_root(wio):
    with pytest.raises(WorkspaceSandboxError):
        await wio.resolve("../outside.txt")


async def test_resolve_follows_symlinks_before_checking_containment(wio, root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (root / "link").symlink_to(outside)
    with pytest.raises(WorkspaceSandboxError):
        await wio.resolve("link/x.txt")


async def test_unbound_resolve_is_absolute_from_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert await LocalWorkspaceIO().resolve("x.txt") == str(tmp_path.resolve() / "x.txt")


async def test_stat(wio, root):
    (root / "f.txt").write_bytes(b"abc")
    (root / "d").mkdir()
    assert await wio.stat(str(root / "f.txt")) == FileStat(
        is_dir=False, size=3, mtime=os.stat(root / "f.txt").st_mtime,
    )
    assert (await wio.stat(str(root / "d"))).is_dir
    assert await wio.stat(str(root / "missing")) is None
    assert await wio.stat(str(root / "nul\x00byte")) is None


async def test_read_whole_or_head(wio, root):
    (root / "f.bin").write_bytes(b"0123456789")
    assert await wio.read(str(root / "f.bin")) == b"0123456789"
    assert await wio.read(str(root / "f.bin"), max_bytes=4) == b"0123"


async def test_write_creates_parents_and_leaves_no_temp_file(wio, root):
    key = str(root / "new" / "deep" / "f.txt")
    await wio.write(key, b"hi")
    assert Path(key).read_bytes() == b"hi"
    assert os.listdir(root / "new" / "deep") == ["f.txt"]


async def test_write_keeps_the_file_mode(wio, root):
    script = root / "run.sh"
    script.write_text("echo 1\n")
    script.chmod(0o755)
    await wio.write(str(script), b"echo 2\n")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert script.read_text() == "echo 2\n"


async def test_delete_and_list_dir(wio, root):
    (root / "a").write_text("")
    (root / "b").write_text("")
    await wio.delete(str(root / "a"))
    assert await wio.list_dir(str(root)) == ["b"]


async def test_local_file_is_the_file_itself(wio, root):
    (root / "doc.pdf").write_bytes(b"%PDF")
    async with wio.local_file(str(root / "doc.pdf")) as local:
        assert local == root / "doc.pdf"


async def test_which(wio):
    assert await wio.which("sh")
    assert not await wio.which("surogates-no-such-command")


def test_workspace_io_from_prefers_the_dispatched_io(root):
    given = LocalWorkspaceIO(workspace_path=str(root))
    assert workspace_io_from({"workspace_io": given, "workspace_path": "/elsewhere"}) is given


def test_workspace_io_from_falls_back_to_workspace_path(root):
    wio = workspace_io_from({"workspace_path": str(root)})
    assert isinstance(wio, LocalWorkspaceIO)
    assert wio.root == str(root)


def test_workspace_io_from_without_a_workspace_is_unbound():
    assert workspace_io_from({}).root is None
