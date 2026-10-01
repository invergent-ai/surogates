"""DeviceWorkspaceIO: WorkspaceIO calls as operations a laptop runs."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from surogates.devices.workspace import (
    MAX_PAYLOAD_BYTES,
    OUTPUT_CAP_CHARS,
    DeviceWorkspaceIO,
)
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import FileStat, LocalWorkspaceIO
from tests.fake_laptop import InProcessRunner


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def runner(root) -> InProcessRunner:
    return InProcessRunner(LocalWorkspaceIO(workspace_path=str(root)))


@pytest.fixture
def wio(runner, root) -> DeviceWorkspaceIO:
    return DeviceWorkspaceIO(runner, root=str(root))


async def test_files_round_trip_through_operations(wio, root):
    key = await wio.resolve("a/b.txt")
    assert key == str(root / "a" / "b.txt")
    await wio.write(key, b"\x00\xffbytes")
    assert (root / "a" / "b.txt").read_bytes() == b"\x00\xffbytes"
    assert await wio.read(key) == b"\x00\xffbytes"
    assert await wio.read(key, max_bytes=2) == b"\x00\xff"
    assert await wio.stat(key) == FileStat(is_dir=False, size=7, mtime=os.stat(key).st_mtime)
    assert await wio.stat(str(root / "missing")) is None
    assert await wio.list_dir(str(root / "a")) == ["b.txt"]
    await wio.delete(key)
    assert not (root / "a" / "b.txt").exists()


async def _same_error(wio, local, call):
    with pytest.raises(OSError) as remote:
        await call(wio)
    with pytest.raises(OSError) as here:
        await call(local)
    assert type(remote.value) is type(here.value)
    assert remote.value.errno == here.value.errno
    assert str(remote.value) == str(here.value)


async def test_os_errors_keep_their_type_errno_and_message(wio, root):
    local = LocalWorkspaceIO(workspace_path=str(root))
    (root / "dir").mkdir()
    locked = root / "locked.txt"
    locked.write_text("x")
    locked.chmod(0)
    try:
        await _same_error(wio, local, lambda io: io.read(str(root / "missing.txt")))
        await _same_error(wio, local, lambda io: io.read(str(root / "dir")))
        if os.geteuid() != 0:
            await _same_error(wio, local, lambda io: io.read(str(locked)))
    finally:
        locked.chmod(0o600)


async def test_other_errors_keep_their_type(wio, root):
    with pytest.raises(WorkspaceSandboxError, match="Path traversal blocked"):
        await wio.resolve("../outside.txt")
    with pytest.raises(ValueError, match="null byte"):
        await wio.read(str(root / "nul\x00byte"))


async def test_commands_and_checks_run_on_the_laptop(wio, root):
    result = await wio.run("echo out; exit 3", workdir=None, timeout=10)
    assert (result.output, result.returncode, result.timed_out) == ("out\n", 3, False)
    assert (await wio.run("printf 'a\\000b'", workdir=None, timeout=10)).output == "a\x00b"
    assert await wio.which("sh")
    assert await wio.check_write("/etc/passwd") == (
        "Write denied: '/etc/passwd' is a protected system/credential file."
    )


async def test_long_command_output_keeps_its_head_and_tail(wio):
    result = await wio.run(
        "printf START; head -c 600000 /dev/zero | tr '\\000' x; printf END",
        workdir=None, timeout=30,
    )
    assert len(result.output) < OUTPUT_CAP_CHARS + 200
    assert result.output.startswith("START") and result.output.endswith("END")
    assert "chars omitted by the computer" in result.output


async def test_a_write_over_the_cap_fails_before_it_is_sent(wio, runner, root):
    with pytest.raises(OSError) as raised:
        await wio.write(str(root / "big.bin"), b"x" * (MAX_PAYLOAD_BYTES + 1))
    assert raised.value.errno == errno.EFBIG
    assert "write" not in runner.kinds
    assert not (root / "big.bin").exists()


@pytest.mark.parametrize("max_bytes", [None, 3 * MAX_PAYLOAD_BYTES])
async def test_a_read_over_the_cap_fails_but_its_head_does_not(wio, root, max_bytes):
    (root / "big.bin").write_bytes(b"y" * (MAX_PAYLOAD_BYTES + 1))
    with pytest.raises(OSError) as raised:
        await wio.read(str(root / "big.bin"), max_bytes=max_bytes)
    assert raised.value.errno == errno.EFBIG
    assert await wio.read(str(root / "big.bin"), max_bytes=8192) == b"y" * 8192


async def test_a_local_file_is_a_private_copy_removed_after_use(wio, root):
    (root / "doc.pdf").write_bytes(b"%PDF-1.7")
    async with wio.local_file(str(root / "doc.pdf")) as local:
        assert local != root / "doc.pdf"
        assert local.suffix == ".pdf"
        assert local.read_bytes() == b"%PDF-1.7"
        copy = local
    assert not copy.exists()
