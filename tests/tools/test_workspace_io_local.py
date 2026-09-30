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


async def test_root_is_the_path_as_given_and_keys_are_resolved(root):
    real = root / "real"
    real.mkdir()
    link = root / "link"
    link.symlink_to(real)
    wio = LocalWorkspaceIO(workspace_path=str(link))
    assert wio.root == str(link)
    assert await wio.resolve("a.txt") == os.path.join(os.path.realpath(real), "a.txt")


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


async def test_run_reports_output_and_exit_code(wio, root):
    result = await wio.run("echo out; echo err >&2; exit 3", workdir=None, timeout=10)
    assert result.output == "out\n\nerr\n"
    assert result.returncode == 3
    assert not result.timed_out


async def test_run_times_out(wio):
    result = await wio.run("sleep 5", workdir=None, timeout=1)
    assert result.timed_out
    assert result.returncode == 124
    assert result.output == "Command timed out after 1 seconds"


async def test_run_sets_home_to_the_root(wio, root):
    result = await wio.run("echo $HOME", workdir=None, timeout=10)
    assert result.output.strip() == str(root)


async def test_run_blocks_a_workdir_outside_the_root(wio):
    with pytest.raises(WorkspaceSandboxError, match="All commands must run within"):
        await wio.run("pwd", workdir="/etc", timeout=10)


async def test_run_blocks_shell_metacharacters_in_workdir(tmp_path):
    with pytest.raises(WorkspaceSandboxError, match="disallowed character"):
        await LocalWorkspaceIO().run("pwd", workdir=str(tmp_path) + ";rm", timeout=10)


async def test_start_returns_a_process_handle(wio, tmp_path, monkeypatch):
    from surogates.tools.utils import process_registry as registry_module

    monkeypatch.setattr(registry_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    started = await wio.start(
        "sleep 5", workdir=None, task_id="t", pty=False,
        notify_on_complete=True, watcher_interval=60,
    )
    session = registry_module.process_registry.get(started["session_id"])
    try:
        assert session.pid == started["pid"]
        assert session.notify_on_complete is True
        assert session.watcher_interval == 60
    finally:
        registry_module.process_registry.kill_process(started["session_id"])


async def test_check_write_refuses_credentials_and_system_paths(wio):
    assert await wio.check_write("/etc/passwd") == (
        "Write denied: '/etc/passwd' is a protected system/credential file."
    )
    assert (await wio.check_write("/etc/hosts")).startswith(
        "Refusing to write to sensitive system path: /etc/hosts"
    )


async def test_check_write_allows_the_workspace(wio, root):
    assert await wio.check_write(str(root / "a.txt")) is None
