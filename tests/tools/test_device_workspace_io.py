"""DeviceWorkspaceIO: WorkspaceIO calls as operations a laptop runs."""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
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
from surogates.tools.workspace_io import FileStat, LinePage, LocalWorkspaceIO, RevisionConflict, RipgrepError
from tests import fake_laptop
from tests.fake_laptop import BAD_PAGE, BAD_WALK, CONFLICT, InProcessRunner, perform


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
    st = os.stat(key)
    assert await wio.stat(key) == FileStat(
        is_dir=False, size=7, mtime=st.st_mtime,
        revision=f"{st.st_dev}:{st.st_ino}:7:{st.st_mtime_ns}:{st.st_ctime_ns}",
    )
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


@pytest.mark.parametrize(
    "value",
    [{"is_dir": False, "size": 1, "mtime": 0.0, "revision": None},
     {"is_dir": False, "size": 1, "mtime": 0.0, "revision": 5},
     {"is_dir": False, "size": 1, "mtime": 0.0}],
    ids=["null revision", "number", "no revision"],
)
async def test_a_stat_without_a_revision_is_an_error_not_a_file_no_write_can_check(value):
    with pytest.raises(DeviceOperationError, match="returned an invalid stat"):
        await DeviceWorkspaceIO(Answering({"ok": value}), root="/").stat("/f")


async def test_a_write_lands_only_on_the_revision_it_expects(wio, root):
    key = str(root / "a.txt")
    (root / "a.txt").write_text("one\n")
    seen = (await wio.stat(key)).revision
    await wio.write(key, b"two\n", expected_revision=seen)
    assert (root / "a.txt").read_text() == "two\n"
    # The file is at another revision now: the same expectation writes nothing.
    with pytest.raises(RevisionConflict) as raised:
        await wio.write(key, b"three\n", expected_revision=seen)
    assert str(raised.value) == CONFLICT.format(key)
    assert (root / "a.txt").read_text() == "two\n"
    assert os.listdir(root) == ["a.txt"]


async def test_a_write_that_expects_a_file_that_is_gone_makes_nothing(wio, root):
    key = str(root / "gone" / "a.txt")
    with pytest.raises(RevisionConflict):
        await wio.write(key, b"x", expected_revision="1:2:3:4:5")
    assert os.listdir(root) == []


async def test_a_write_too_large_for_a_frame_checks_its_revision_too(wio, root):
    key = str(root / "big.bin")
    (root / "big.bin").write_bytes(b"old")
    stale = (await wio.stat(key)).revision
    (root / "big.bin").write_bytes(b"newer")
    with pytest.raises(RevisionConflict):
        await wio.write(key, os.urandom(MAX_PAYLOAD_BYTES + 1), expected_revision=stale)
    assert (root / "big.bin").read_bytes() == b"newer"


async def test_two_writes_on_one_revision_land_once(wio, root):
    key = str(root / "a.txt")
    (root / "a.txt").write_text("one\n")
    seen = (await wio.stat(key)).revision
    writes = [b"first\n", b"second\n"]
    outcomes = await asyncio.gather(
        *(wio.write(key, data, expected_revision=seen) for data in writes), return_exceptions=True,
    )
    [landed] = [data for data, outcome in zip(writes, outcomes) if outcome is None]
    assert [type(outcome) for outcome in outcomes if outcome is not None] == [RevisionConflict]
    assert (root / "a.txt").read_bytes() == landed


async def test_a_writes_expected_revision_goes_beside_its_data_or_its_transfer(root):
    runner = Recording()
    wio = DeviceWorkspaceIO(runner, root=str(root))
    data = os.urandom(MAX_PAYLOAD_BYTES + 1)
    await wio.write(str(root / "big.bin"), data, expected_revision="1:2:3:4:5")
    await wio.write(str(root / "small.bin"), b"x", expected_revision="6:7:8:9:10")
    assert runner.asked == [
        ("write", {"key": str(root / "big.bin"), "transfer": {
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }, "expected_revision": "1:2:3:4:5"}, data),
        ("write", {"key": str(root / "small.bin"), "data": "eA==", "expected_revision": "6:7:8:9:10"}, None),
    ]


async def test_a_conflict_is_an_os_error_of_its_own(root):
    wio = DeviceWorkspaceIO(Answering({"error": {"type": "conflict", "message": "changed"}}), root=str(root))
    with pytest.raises(RevisionConflict) as raised:
        await wio.write(str(root / "a.txt"), b"x", expected_revision="1:2:3:4:5")
    # An OSError, so a V4A patch reports it as that file's error and goes on with the others.
    assert isinstance(raised.value, OSError) and raised.value.errno is None
    assert str(raised.value) == "changed"


async def test_a_walk_lists_the_files_under_a_folder_with_their_sizes(wio, root):
    (root / "sub").mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "sub" / "b.md").write_text("b")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "i.js").write_text("")
    (root / "link").symlink_to(root / "a.txt")
    walked = await wio.walk(await wio.resolve("."), skip={"node_modules"})
    assert sorted(walked.files) == [("a.txt", 5), ("sub/b.md", 1)]
    assert walked.truncated is False
    assert walked.cursor.isdigit()
    assert sorted((await wio.walk(str(root / "sub"), skip=())).files) == [("b.md", 1)]


async def test_a_walk_since_a_cursor_lists_only_what_changed_after_it(wio, root):
    (root / "old.txt").write_text("o")
    # Past the cursor's margin, which covers a filesystem's coarser clock.
    await asyncio.sleep(workspace.WALK_MARGIN_NS / 1e9 + 0.1)
    first = await wio.walk(str(root), skip=())
    (root / "new.txt").write_text("n")
    assert (await wio.walk(str(root), skip=(), since=first.cursor)).files == [("new.txt", 1)]


async def test_a_walk_enters_no_folder_the_tree_hides(wio, root):
    for name in ("src", ".cache", ".github", "_whiteboard", "sub/_whiteboard", "node_modules"):
        (root / name).mkdir(parents=True)
        (root / name / "f.txt").write_text("x")
    walked = await wio.walk(str(root), skip={"node_modules"}, skip_top={"_whiteboard"}, skip_hidden=True)
    assert sorted(walked.files) == [(".github/f.txt", 1), ("src/f.txt", 1), ("sub/_whiteboard/f.txt", 1)]


async def test_a_walk_leaves_out_a_name_that_is_not_utf_8_and_lists_its_decoded_twin_once(wio, root):
    (root / "a.txt").write_text("a")
    with open(os.path.join(os.fsencode(root), b"n\xff"), "wb") as fh:
        fh.write(b"bytes")
    (root / "n\ufffd").write_text("t")
    assert sorted((await wio.walk(str(root), skip=())).files) == [("a.txt", 1), ("n\ufffd", 1)]


# Swaps the folder at argv[1] for a link to argv[3] and back, as a command in the VM writing the folder could,
# until it is killed.  Each swap is a line on stdout.
_SWAPPER = """
import os, sys
folder, real, outside = sys.argv[1:4]
while True:
    try:
        os.rename(folder, real); os.symlink(outside, folder); os.unlink(folder); os.rename(real, folder)
        print(flush=True)
    except OSError:
        pass
"""


async def test_a_walk_never_enters_a_folder_swapped_for_a_link_meanwhile(wio, root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "o.txt").write_text("outside")
    (root / "aaa").mkdir()
    (root / "aaa" / "in.txt").write_text("in")
    swapper = subprocess.Popen(
        [sys.executable, "-c", _SWAPPER, str(root / "aaa"), str(root / "aaa.real"), str(outside)],
        stdout=subprocess.PIPE,
    )
    try:
        assert swapper.stdout.readline() == b"\n"  # swapping
        leaked, deadline = set(), time.monotonic() + 2
        while time.monotonic() < deadline:
            leaked |= {path for path, _ in (await wio.walk(str(root), skip=())).files if path == "aaa/o.txt"}
    finally:
        swapper.kill()
        swapper.wait()
    assert leaked == set()


async def test_the_reference_laptops_walk_stops_once_its_time_is_up_and_says_so(root, monkeypatch):
    (root / "a.txt").write_text("a")
    monkeypatch.setattr(fake_laptop, "WALK_BUDGET_S", -1)  # up before the first entry
    walked = await perform(LocalWorkspaceIO(str(root)), "walk", {
        "key": str(root), "skip": [], "skip_top": [], "skip_hidden": False, "since": None,
    })
    assert walked["ok"]["files"] == [] and walked["ok"]["truncated"] is True


async def test_a_walk_reads_a_folder_no_further_than_it_looks_and_closes_every_folder_it_opened(wio, root, monkeypatch):
    (root / "deep" / "many").mkdir(parents=True)
    for name in range(2 * workspace.MAX_WALK_FILES):
        (root / "deep" / "many" / str(name)).touch()
    scandir, read = os.scandir, 0

    class Counting:
        """os.scandir, counting the entries it yields."""

        def __init__(self, path):
            self._listing = scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal read
            entry = next(self._listing)
            read += 1
            return entry

        def close(self):
            self._listing.close()

    handles = len(os.listdir("/proc/self/fd"))
    monkeypatch.setattr(os, "scandir", Counting)
    walked = await wio.walk(str(root), skip=())
    monkeypatch.undo()
    assert walked.truncated is True
    assert read < workspace.MAX_WALK_FILES + 10
    assert len(os.listdir("/proc/self/fd")) == handles


def _chain(top: Path, depth: int) -> None:
    """*depth* folders, each in the last, under *top*, a file at the bottom: built through handles, as a command
    in the VM could build it, past what a path can name."""
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        os.close(os.open("bottom.txt", os.O_WRONLY | os.O_CREAT, dir_fd=fd))
    finally:
        os.close(fd)


async def test_a_walk_goes_no_deeper_than_its_depth_and_says_so(wio, root):
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    # Two handles a level, as the app holds them: raised as Node raises its own.
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    whole, deep = root / "whole", root / "deep"
    whole.mkdir()
    deep.mkdir()
    try:
        _chain(whole, workspace.MAX_WALK_DEPTH)
        _chain(deep, workspace.MAX_WALK_DEPTH + 1)
        walked = await wio.walk(str(whole), skip=())
        assert (walked.files, walked.truncated) == ([("d/" * workspace.MAX_WALK_DEPTH + "bottom.txt", 0)], False)
        walked = await wio.walk(str(deep), skip=())
        assert (walked.files, walked.truncated) == ([], True)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        subprocess.run(["rm", "-rf", str(whole), str(deep)], check=True)


async def test_a_walk_stops_at_a_folder_it_cannot_enter_and_says_so(wio, root, monkeypatch):
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("b")
    handles = len(os.listdir("/proc/self/fd"))
    real_open, opened = os.open, 0

    def out_of_handles(*args, **kwargs):
        nonlocal opened
        opened += 1
        if opened == 2:  # the key's, then sub's
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", out_of_handles)
    walked = await wio.walk(str(root), skip=())
    monkeypatch.undo()
    # What it could not enter is unknown: not said whole.
    assert walked.truncated is True
    assert ("sub/b.txt", 1) not in walked.files
    assert len(os.listdir("/proc/self/fd")) == handles


async def test_a_walk_stops_at_its_cap_and_says_so(wio, root):
    (root / "many").mkdir()
    for name in range(workspace.MAX_WALK_FILES + 1):
        (root / "many" / str(name)).touch()
    walked = await wio.walk(str(root), skip=())
    assert len(walked.files) == workspace.MAX_WALK_FILES
    assert walked.truncated is True


async def test_a_walk_lists_a_folder_of_exactly_its_cap_whole_and_says_so(wio, root):
    for name in range(workspace.MAX_WALK_FILES):
        (root / str(name)).touch()
    walked = await wio.walk(str(root), skip=())
    assert len(walked.files) == workspace.MAX_WALK_FILES
    assert walked.truncated is False


async def test_a_walk_stops_where_its_paths_fill_one_frame(wio, root):
    # Each path costs 252 encoded and 24 more: 3 799 of them fit in MAX_PAYLOAD_BYTES, not 3 800.
    for name in range(4_000):
        (root / str(name).rjust(250, "x")).touch()
    walked = await wio.walk(str(root), skip=())
    assert len(walked.files) == 3_799
    assert walked.truncated is True


async def test_a_walk_stops_past_its_looks_a_folder_it_does_not_enter_counted_too(wio, root):
    # Hidden, so none is entered: each is one look.
    for name in range(workspace.MAX_WALK_LOOKS):
        (root / f".{name}").mkdir()
    walked = await wio.walk(str(root), skip=(), skip_hidden=True)
    assert (walked.files, walked.truncated) == ([], False)
    (root / ".one-more").mkdir()
    walked = await wio.walk(str(root), skip=(), skip_hidden=True)
    assert (walked.files, walked.truncated) == ([], True)


async def test_a_walk_fails_as_its_folder_does(wio, root):
    (root / "a.txt").write_text("a")
    with pytest.raises(NotADirectoryError):
        await wio.walk(str(root / "a.txt"), skip=())
    with pytest.raises(FileNotFoundError):
        await wio.walk(str(root / "missing"), skip=())


@pytest.mark.parametrize(
    "value",
    [None, [], {"files": [], "truncated": False}, {"files": [["a", "1"]], "truncated": False, "cursor": "1"},
     {"files": [["a", -1]], "truncated": False, "cursor": "1"}, {"files": [["a"]], "truncated": False, "cursor": "1"},
     {"files": [[1, 1]], "truncated": False, "cursor": "1"}, {"files": [], "truncated": 0, "cursor": "1"},
     {"files": [], "truncated": False, "cursor": 1}, {"files": [], "truncated": False, "cursor": ""},
     {"files": [], "truncated": False, "cursor": "soon"}, {"files": [], "truncated": False, "cursor": "\u0661\u0662"},
     {"files": [], "truncated": False, "cursor": "1" * 21}],
    ids=["null", "list", "no cursor", "size as text", "negative size", "no size", "path as number",
         "truncated as number", "cursor as number", "empty cursor", "cursor as words", "cursor in other digits",
         "cursor too long"],
)
async def test_a_listing_that_is_not_one_is_an_error_not_a_wrong_tree(value):
    with pytest.raises(DeviceOperationError, match="returned an invalid listing"):
        await DeviceWorkspaceIO(_Answers(value), root="/").walk("/", skip=())


@pytest.mark.parametrize("skips", [{"skip": "node_modules"}, {"skip": (), "skip_top": "_whiteboard"}], ids=["skip", "skip_top"])
async def test_a_walk_takes_folder_names_not_one_string(skips):
    # A string is a collection of its characters: sorted, it would skip every one-letter folder.
    with pytest.raises(TypeError, match="folder names"):
        await DeviceWorkspaceIO(_Answers({"files": [], "truncated": False, "cursor": "1"}), root="/").walk("/", **skips)


async def test_the_reference_laptop_refuses_a_walk_it_cannot_take(root):
    taken = {"key": str(root), "skip": [], "skip_top": [], "skip_hidden": False, "since": None}
    for changes in ({"skip": "node_modules"}, {"skip_top": [1]}, {"skip_hidden": 1}, {"since": "yesterday"}, {"since": "1" * 21}):
        assert await perform(LocalWorkspaceIO(str(root)), "walk", {**taken, **changes}) == {
            "error": {"type": "value", "message": BAD_WALK},
        }
    missing = {name: value for name, value in taken.items() if name != "since"}
    assert await perform(LocalWorkspaceIO(str(root)), "walk", missing) == {"error": {"type": "value", "message": BAD_WALK}}
    # The key before anything else, as the app checks it.
    assert await perform(LocalWorkspaceIO(str(root)), "walk", {**taken, "key": 7, "skip": "x"}) == {
        "error": {"type": "value", "message": "'key' must be a string"},
    }
    assert (await perform(LocalWorkspaceIO(str(root)), "walk", {**taken, "since": "9" * 20}))["ok"]["files"] == []
