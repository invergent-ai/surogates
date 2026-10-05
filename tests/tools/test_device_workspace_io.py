"""DeviceWorkspaceIO: WorkspaceIO calls as operations a laptop runs."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from surogates.devices import workspace
from surogates.devices.workspace import (
    MAX_MESSAGE_CHARS,
    MAX_PAYLOAD_BYTES,
    MAX_READ_BYTES,
    MAX_WRITE_BYTES,
    OUTPUT_CAP_CHARS,
    READ_TOO_LARGE,
    WRITE_TOO_LARGE,
    DeviceOperationError,
    DeviceWorkspaceIO,
)
from surogates.tools.builtin import file_ops
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import FileStat, LinePage, LocalWorkspaceIO, RipgrepError
from tests.fake_laptop import BAD_PAGE, InProcessRunner, perform


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

    async def run(self, kind, args, payload=None):
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
        await wio.write(str(root / "big.bin"), b"x" * (MAX_WRITE_BYTES + 1))
    assert raised.value.errno == errno.EFBIG
    assert str(raised.value) == f"[Errno 27] {WRITE_TOO_LARGE}"
    assert "write" not in runner.kinds
    assert not (root / "big.bin").exists()


class Recording:
    """A runner that answers every operation ok, and keeps what it was asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict, bytes | None]] = []

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
        self.asked.append((kind, args, payload))
        return {"ok": None}


async def test_a_write_of_more_than_one_frame_is_named_by_its_content(root):
    runner = Recording()
    wio = DeviceWorkspaceIO(runner, root=str(root))
    data = os.urandom(MAX_PAYLOAD_BYTES + 1)
    await wio.write(str(root / "big.bin"), data)
    await wio.write(str(root / "exact.bin"), data[:MAX_PAYLOAD_BYTES])
    # Named by its size and SHA-256, so a resumed call asks for the same operation; the bytes go beside the args.
    assert runner.asked == [
        ("write", {"key": str(root / "big.bin"), "transfer": {
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }}, data),
        ("write", {"key": str(root / "exact.bin"), "data": base64.b64encode(data[:MAX_PAYLOAD_BYTES]).decode()}, None),
    ]


async def test_a_write_of_up_to_50_mib_lands_whole(wio, root):
    data = os.urandom(MAX_WRITE_BYTES)
    await wio.write(str(root / "most.bin"), data)
    assert (root / "most.bin").read_bytes() == data


async def test_a_large_write_is_named_off_the_event_loop(root, monkeypatch):
    namers = []
    transfer_for = workspace._transfer_for

    def recorded(data):
        namers.append(threading.current_thread())
        return transfer_for(data)

    monkeypatch.setattr(workspace, "_transfer_for", recorded)
    await DeviceWorkspaceIO(Recording(), root=str(root)).write(str(root / "big.bin"), b"x" * (MAX_PAYLOAD_BYTES + 1))
    # Up to 50 MiB to hash: in a thread, not on the loop the worker's other sessions share.
    assert namers and threading.current_thread() not in namers


async def test_args_too_large_for_one_operation_are_refused_in_words_that_fit_any_kind(wio, runner):
    # No write reaches this any more: its data goes as a transfer past 1 MiB.
    with pytest.raises(OSError) as raised:
        await wio.run("x" * MAX_MESSAGE_CHARS, workdir=None, timeout=10)
    assert str(raised.value) == "[Errno 27] Too large for one operation on a local folder (over 1.5 MiB)"
    assert runner.kinds == []


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

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
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


async def test_a_local_file_is_written_off_the_loop_and_its_bytes_are_not_held_through_the_parse(
    wio, root, monkeypatch,
):
    (root / "doc.pdf").write_bytes(b"%PDF-1.7")
    writers = []
    write_bytes = Path.write_bytes

    def recorded(path, data):
        writers.append(threading.current_thread())
        return write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", recorded)
    copying = wio.local_file(str(root / "doc.pdf"))
    async with copying as local:
        assert local.read_bytes() == b"%PDF-1.7"
        # Up to 50 MiB: written in a thread, and held only in the file while the caller parses it.
        assert writers and threading.current_thread() not in writers
        assert "data" not in copying.gen.ag_frame.f_locals


PAGE = {"encoding": "utf-8", "offset": 1, "limit": 2000, "max_bytes": 8192}


async def test_a_page_crosses_as_its_bytes_and_line_count(wio, root):
    (root / "u.txt").write_bytes("\ufeffone\r\ntwo\nthree".encode("utf-16-le"))
    asked = {**PAGE, "encoding": "utf-16-le", "offset": 2}
    page = await wio.read_lines(str(root / "u.txt"), **asked)
    assert page == LinePage("two\nthree".encode("utf-16-le"), 3)
    assert page == await LocalWorkspaceIO(str(root)).read_lines(str(root / "u.txt"), **asked)


class Paging:
    """A runner that answers every operation with an empty page, and keeps what it was asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict]] = []

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
        self.asked.append((kind, args))
        return {"ok": {"data": "", "total_lines": 0}}


async def test_a_page_asks_for_at_most_one_frame_of_data(root):
    runner = Paging()
    page = await DeviceWorkspaceIO(runner, root=str(root)).read_lines(
        str(root / "a.txt"), encoding="utf-32-le", offset=3, limit=-2, max_bytes=8 * MAX_PAYLOAD_BYTES,
    )
    assert page == LinePage(b"", 0)
    # So a page is always the ok value itself, never a transfer.
    assert runner.asked == [("read_lines", {
        "key": str(root / "a.txt"), "encoding": "utf-32-le", "offset": 3, "limit": -2, "max_bytes": MAX_PAYLOAD_BYTES,
    })]


def sparse(path: Path, size: int) -> str:
    with open(path, "wb") as fh:
        fh.truncate(size)
    return str(path)


async def test_a_full_page_crosses_in_one_message(wio, root):
    # InProcessRunner checks that the reply fits one message.
    (root / "line.txt").write_bytes(b"x" * (MAX_PAYLOAD_BYTES + 10))
    page = await wio.read_lines(str(root / "line.txt"), **{**PAGE, "max_bytes": 4 * MAX_PAYLOAD_BYTES})
    assert page == LinePage(b"x" * MAX_PAYLOAD_BYTES, 1)


async def test_a_page_of_a_file_at_the_cap_is_paged(wio, root):
    assert await wio.read_lines(sparse(root / "most.log", MAX_READ_BYTES), **PAGE) == LinePage(b"\0" * 8192, 1)


async def test_a_page_of_a_file_over_the_cap_fails_before_it_is_scanned(wio, root, monkeypatch):
    sparse(root / "huge.log", MAX_READ_BYTES + 1)

    async def scanned(*args, **kwargs):
        raise AssertionError("the file was scanned")

    monkeypatch.setattr(LocalWorkspaceIO, "read_lines", scanned)
    with pytest.raises(OSError) as raised:
        await wio.read_lines(str(root / "huge.log"), **PAGE)
    assert str(raised.value) == f"[Errno 27] {READ_TOO_LARGE}"


async def test_a_page_fails_as_the_cloud_fails(wio, root):
    local = LocalWorkspaceIO(workspace_path=str(root))
    (root / "dir").mkdir()
    locked = root / "locked.txt"
    locked.write_text("x")
    locked.chmod(0)
    try:
        await _same_error(wio, local, lambda io: io.read_lines(str(root / "missing.txt"), **PAGE))
        await _same_error(wio, local, lambda io: io.read_lines(str(root / "dir"), **PAGE))
        if os.geteuid() != 0:
            await _same_error(wio, local, lambda io: io.read_lines(str(locked), **PAGE))
    finally:
        locked.chmod(0o600)


@pytest.mark.parametrize(
    "args",
    [{"encoding": "latin-1"}, {"encoding": None}, {"offset": 0}, {"limit": 1.5}, {"max_bytes": -1},
     # The app's JSON.parse reads these as Infinity and -Infinity, which are no integers.
     {"offset": 9 * 10**308}, {"limit": -(2**1024 - 2**970)}],
    ids=["unknown encoding", "no encoding", "offset 0", "fractional limit", "negative max_bytes",
         "offset read as infinite", "limit read as infinite"],
)
async def test_a_page_the_computer_cannot_take_is_refused(wio, root, args):
    (root / "a.txt").write_text("one\n")
    with pytest.raises(ValueError) as raised:
        await wio.read_lines(str(root / "a.txt"), **{**PAGE, **args})
    assert str(raised.value) == BAD_PAGE


@pytest.mark.parametrize(
    "value",
    [None, "", [], {"total_lines": 0}, {"data": "", "total_lines": "1"}, {"data": "", "total_lines": True},
     {"data": "AA", "total_lines": 1}, {"data": 7, "total_lines": 1}, {"data": "", "total_lines": -1},
     {"data": base64.b64encode(b"x" * (PAGE["max_bytes"] + 1)).decode(), "total_lines": 1}],
    ids=["null", "string", "list", "no data", "count as text", "count as bool", "unpadded data", "data as number",
         "negative count", "data over max_bytes"],
)
async def test_a_page_that_is_not_one_is_an_error_not_a_wrong_page(value):
    with pytest.raises(DeviceOperationError, match="returned an invalid page"):
        await DeviceWorkspaceIO(_Answers(value), root="/").read_lines("/f", **PAGE)


async def test_a_number_the_app_reads_as_finite_is_paged(wio, root):
    # 309 digits, and the largest double: JSON.parse reads both as integers.
    (root / "a.txt").write_text("one\n")
    asked = {**PAGE, "offset": 10**308, "limit": -(2**1024 - 2**970 - 1)}
    assert await wio.read_lines(str(root / "a.txt"), **asked) == LinePage(b"", 1)


async def test_the_reference_laptop_takes_no_bool_for_an_integer(root):
    # The worker sends a bool as an int; the app refuses true, as Number.isInteger(true) is false.
    (root / "a.txt").write_text("one\n")
    args = {"key": str(root / "a.txt"), **PAGE, "limit": True}
    assert await perform(LocalWorkspaceIO(str(root)), "read_lines", args) == {"error": {"type": "value", "message": BAD_PAGE}}


async def test_a_bool_offset_or_limit_reads_as_the_cloud_reads_it(wio, root):
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    asked = {**PAGE, "offset": True, "limit": True}
    page = await wio.read_lines(str(root / "a.txt"), **asked)
    assert page == LinePage(b"one\n", 3)
    assert page == await LocalWorkspaceIO(str(root)).read_lines(str(root / "a.txt"), **asked)
