"""DeviceWorkspaceIO: WorkspaceIO calls as operations a laptop runs."""

from __future__ import annotations

import errno
import json
import os
import shutil
from pathlib import Path

import pytest

from surogates.devices.workspace import (
    MAX_PAYLOAD_BYTES,
    MAX_READ_BYTES,
    OUTPUT_CAP_CHARS,
    READ_TOO_LARGE,
    DeviceOperationError,
    DeviceWorkspaceIO,
)
from surogates.tools.builtin import file_ops
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import FileStat, LocalWorkspaceIO, RipgrepError
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


@pytest.mark.parametrize(
    ("command", "kept"),
    [
        ("head -c 600000 /dev/zero", "\x00"),
        # Every CJK character is 6 characters once JSON-encoded.
        ("yes 中 | head -n 600000 | tr -d '\\n'", "中"),
    ],
    ids=["nul", "non-ascii"],
)
async def test_output_is_capped_by_its_encoded_size(wio, command, kept):
    result = await wio.run(command, workdir=None, timeout=30)
    assert "chars omitted by the computer" in result.output
    assert result.output.startswith(kept) and result.output.endswith(kept)
    assert OUTPUT_CAP_CHARS // 2 < len(json.dumps(result.output)) < OUTPUT_CAP_CHARS + 200


class _Answers:
    """A laptop that answers every operation with one fixed result."""

    def __init__(self, value) -> None:
        self.value = value

    async def run(self, kind, args):
        return {"ok": self.value}


async def test_data_in_standard_base64_is_decoded():
    laptop = DeviceWorkspaceIO(_Answers("+/8="), root="/")
    assert await laptop.read("/f") == b"\xfb\xff"


@pytest.mark.parametrize(
    "value",
    ["-__-", "-_8=", "AA", "AAAA\nAAAA", "AA AA", "é", None, 7],
    ids=["base64url", "base64url padded", "unpadded", "line break", "space", "non-ascii", "null", "number"],
)
async def test_data_that_is_not_standard_base64_is_an_error_not_a_corrupt_read(value):
    # A lenient decode drops what it does not know, and a patch would write the result back.
    laptop = DeviceWorkspaceIO(_Answers(value), root="/")
    with pytest.raises(DeviceOperationError, match="invalid data"):
        await laptop.read("/f")


class _Folder:
    def __init__(self, output: str) -> None:
        self.output = output

    async def ripgrep(self, key, **options):
        return self.output


async def _search(output: str) -> str:
    laptop = DeviceWorkspaceIO(InProcessRunner(_Folder(output)), root="/")
    return await laptop.ripgrep("/", mode="count", pattern="p")


@pytest.mark.parametrize(
    "line", ["a" * 59 + "\n", "\x00" * 49 + "\n", "中" * 9 + "\n"], ids=["ascii", "nul", "non-ascii"],
)
async def test_search_output_over_the_cap_is_refused_not_cut(line):
    with pytest.raises(RipgrepError, match="narrow the pattern"):
        await _search(line * (600_000 // len(line)))


async def test_search_output_within_the_cap_comes_back_whole():
    text = "a" * (OUTPUT_CAP_CHARS - 4) + "\n"  # encoded: quotes, the text, and "\n" as two characters
    assert await _search(text) == text
    with pytest.raises(RipgrepError):
        await _search(text + "a")


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
@pytest.mark.parametrize(
    "arguments",
    [{}, {"output_mode": "count"}, {"target": "files", "pattern": "*.txt"}],
    ids=["content", "count", "files"],
)
async def test_the_search_tool_reports_a_search_too_large_for_one_operation(wio, root, arguments):
    for number in range(8000):
        (root / f"match-{number}.txt").write_text("needle\n")
    raw = await file_ops._search_files_handler(
        {"pattern": "needle", "path": str(root), "limit": 100_000, **arguments},
        workspace_io=wio, task_id="search-too-large",
    )
    error = json.loads(raw)["error"]
    assert "Search failed" in error and "narrow the pattern" in error


async def test_a_write_over_the_cap_fails_before_it_is_sent(wio, runner, root):
    with pytest.raises(OSError) as raised:
        await wio.write(str(root / "big.bin"), b"x" * (MAX_PAYLOAD_BYTES + 1))
    assert raised.value.errno == errno.EFBIG
    assert "write" not in runner.kinds
    assert not (root / "big.bin").exists()


async def test_a_read_of_more_than_one_frame_comes_back_whole(wio, root):
    data = os.urandom(2 * MAX_PAYLOAD_BYTES + 3)
    (root / "big.bin").write_bytes(data)
    assert await wio.read(str(root / "big.bin")) == data


@pytest.mark.parametrize("max_bytes", [None, 3 * MAX_READ_BYTES])
async def test_a_read_over_the_cap_fails_but_its_head_does_not(wio, root, max_bytes):
    (root / "huge.bin").write_bytes(b"y" * (MAX_READ_BYTES + 1))
    with pytest.raises(OSError) as raised:
        await wio.read(str(root / "huge.bin"), max_bytes=max_bytes)
    assert raised.value.errno == errno.EFBIG
    assert str(raised.value) == f"[Errno 27] {READ_TOO_LARGE}"
    assert await wio.read(str(root / "huge.bin"), max_bytes=8192) == b"y" * 8192


class Answering:
    """A runner that answers every operation with one outcome."""

    def __init__(self, outcome: dict) -> None:
        self.outcome = outcome

    async def run(self, kind: str, args: dict) -> dict:
        return self.outcome


async def test_a_read_answered_with_its_bytes_takes_them_as_they_are(root):
    # The journal's runner hands over a transfer's data this way.
    wio = DeviceWorkspaceIO(Answering({"ok": b"\x00raw"}), root=str(root))
    assert await wio.read(str(root / "x")) == b"\x00raw"


async def test_a_local_file_is_a_private_copy_removed_after_use(wio, root):
    (root / "doc.pdf").write_bytes(b"%PDF-1.7")
    async with wio.local_file(str(root / "doc.pdf")) as local:
        assert local != root / "doc.pdf"
        assert local.suffix == ".pdf"
        assert local.read_bytes() == b"%PDF-1.7"
        copy = local
    assert not copy.exists()
