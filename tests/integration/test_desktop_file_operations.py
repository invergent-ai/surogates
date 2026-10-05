"""The desktop app's file operations against the cloud's: the same answers from both.

Each case runs on the real app, over the real device link and inside its
sandbox, and on the Python reference laptop (``tests/fake_laptop.perform`` over
``LocalWorkspaceIO``), against one folder. Where the laptop is stricter by
design, the case says so and asserts the laptop's own answer.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from surogates.devices.operations import OperationRequest
from surogates.devices.workspace import MAX_PAYLOAD_BYTES, MAX_READ_BYTES
from surogates.tools.workspace_io import RevisionConflict
from surogates.tools.workspace_io.local import CODE_UNITS, LocalWorkspaceIO
from tests.fake_laptop import CONFLICT, perform
from tests.tools.test_workspace_io_read_lines import text

from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    api,
    device_io,
    laptop_rig,
    link_url,
    request_for,
)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
def journal_dir():
    """Where the app keeps its journal and data. srt's sockets go under it, and a
    unix socket's path holds at most 107 bytes, which pytest's tmp_path is too long for."""
    path = Path(tempfile.mkdtemp(prefix="sd-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def prepare(base: Path) -> Path:
    folder = base / "folder"
    (folder / "sub" / "deep").mkdir(parents=True)
    (folder / "special").mkdir()
    (folder / "a.txt").write_text("alpha\nbeta\n")
    (folder / "sub" / "b.txt").write_text("beta gamma\n")
    (folder / "sub" / "deep" / "c.md").write_text("gamma\n")
    (folder / "My Files ü.txt").write_text("unicode\n")
    (folder / "big.bin").write_bytes(b"x" * (MAX_PAYLOAD_BYTES + 10))
    (folder / "huge.bin").write_bytes(b"x" * (MAX_READ_BYTES + 1))
    (folder / "run.sh").write_text("#!/bin/sh\n")
    (folder / "run.sh").chmod(0o755)
    (folder / "special" / "locked.txt").write_text("secret")
    (folder / "special" / "locked.txt").chmod(0o000)
    os.mkfifo(folder / "special" / "pipe")
    outside = base / "outside"
    outside.mkdir()
    (outside / "o.txt").write_text("outside\n")
    os.symlink("a.txt", folder / "link-in")
    os.symlink(outside / "o.txt", folder / "link-out")
    os.link(outside / "o.txt", folder / "hard.txt")
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("[core]\n")
    pages = folder / "pages"
    pages.mkdir()
    (pages / "mixed.txt").write_bytes(b"one\ntwo\r\nthree\rfour")
    (pages / "long.txt").write_bytes(b"x" * 100 + b"\nshort\n")
    # 上 (U+4E0A) and 不 (U+4E0D) have units that hold 0x0A and 0x0D, and end no line.
    for name, encoding in [("u16le", "utf-16-le"), ("u16be", "utf-16-be"), ("u32le", "utf-32-le"), ("u32be", "utf-32-be")]:
        (pages / f"{name}.txt").write_bytes("\ufeff上\r\n不\nlast".encode(encoding))
    (pages / "sig.txt").write_bytes("\ufeffone\ntwo\n".encode())
    # A CR LF across the app's first two pieces of the file.
    (pages / "edge.txt").write_bytes(b"x" * (MAX_PAYLOAD_BYTES - 1) + b"\r\ny\r\n")
    return folder.resolve()


def fill(value, folder: Path):
    if isinstance(value, str):
        return value.replace("{f}", str(folder))
    if isinstance(value, dict):
        return {key: fill(item, folder) for key, item in value.items()}
    return value


def comparable(kind: str, args: dict, outcome: dict) -> dict:
    """What must match: listings and searches in any order, rg's timings aside, and a
    read's data too large for one frame as the transfer the app names it by."""
    if "error" in outcome:
        return outcome
    if kind == "read" and isinstance(outcome["ok"], str):
        data = base64.b64decode(outcome["ok"])
        if len(data) > MAX_PAYLOAD_BYTES:
            return {"ok": {"transfer": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}
    if kind == "list_dir":
        return {"ok": sorted(outcome["ok"])}
    if kind == "ripgrep":
        lines = outcome["ok"].splitlines()
        if args["mode"] == "json":
            events = [json.loads(line) for line in lines]
            for event in events:
                if event["type"] == "end":
                    event["data"].pop("stats", None)
            lines = [json.dumps(event, sort_keys=True) for event in events if event["type"] != "summary"]
        return {"ok": sorted(lines)}
    return outcome


async def on_app(rig, kind: str, args: dict) -> dict:
    request = OperationRequest(**{**_fields(request_for(rig.device_id, rig.root)), "kind": kind, "args": args})
    return await asyncio.wait_for(rig.ops.run(request), 30.0)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


PAGE = {"encoding": "utf-8", "offset": 1, "limit": 2000, "max_bytes": 200_000}

SAME = [
    ("resolve", {"path": "a.txt"}),
    ("resolve", {"path": ""}),
    ("resolve", {"path": "sub/../a.txt"}),
    ("resolve", {"path": "sub/deep/"}),
    ("resolve", {"path": "link-in"}),
    ("resolve", {"path": "link-out"}),
    ("resolve", {"path": "../x"}),
    ("resolve", {"path": "/etc/passwd"}),
    ("resolve", {"path": "~/x"}),
    ("resolve", {"path": "missing/deep/file"}),
    ("resolve", {"path": "My Files ü.txt"}),
    ("resolve", {"path": "{f}/a.txt"}),
    ("resolve", {"path": "{f}2/x"}),
    ("resolve", {"path": "a\0b"}),
    ("check_write", {"path": "/etc/passwd"}),
    ("check_write", {"path": "/etc/hosts"}),
    ("check_write", {"path": "~/.ssh/id_rsa"}),
    ("check_write", {"path": "~/.bashrc"}),
    ("check_write", {"path": "/run/docker.sock"}),
    ("check_write", {"path": "a.txt"}),
    ("check_write", {"path": "sub/new.txt"}),
    ("check_write", {"path": "a\0b"}),
    ("stat", {"key": "{f}/a.txt"}),
    ("stat", {"key": "{f}/sub"}),
    ("stat", {"key": "{f}"}),
    ("stat", {"key": "{f}/missing"}),
    ("stat", {"key": "{f}/special/pipe"}),
    # The helper's sandbox binds the folder in place: each revision matches the one Python makes outside it.
    ("stat", {"key": "{f}/.git/config"}),
    ("stat", {"key": "{f}/special/locked.txt"}),
    ("stat", {"key": "{f}/hard.txt"}),
    ("read", {"key": "{f}/a.txt", "max_bytes": None}),
    ("read", {"key": "{f}/a.txt", "max_bytes": 3}),
    ("read", {"key": "{f}/a.txt", "max_bytes": 0}),
    ("read", {"key": "{f}/big.bin", "max_bytes": None}),
    ("read", {"key": "{f}/big.bin", "max_bytes": 8192}),
    ("read", {"key": "{f}/huge.bin", "max_bytes": None}),
    ("read", {"key": "{f}/huge.bin", "max_bytes": 8192}),
    ("read", {"key": "{f}/sub", "max_bytes": None}),
    ("read", {"key": "{f}/missing", "max_bytes": None}),
    ("read", {"key": "{f}/special/locked.txt", "max_bytes": None}),
    ("read", {"key": "{f}/hard.txt", "max_bytes": None}),
    ("read", {"key": "{f}/My Files ü.txt", "max_bytes": None}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "offset": 2, "limit": 2}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "offset": 4}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "offset": 5}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "limit": 0}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "limit": -1}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "offset": 2, "limit": -1}),
    ("read_lines", {"key": "{f}/pages/long.txt", **PAGE, "max_bytes": 10}),
    ("read_lines", {"key": "{f}/pages/long.txt", **PAGE, "max_bytes": 0}),
    ("read_lines", {"key": "{f}/pages/u16le.txt", **PAGE, "encoding": "utf-16-le", "offset": 2}),
    ("read_lines", {"key": "{f}/pages/u16be.txt", **PAGE, "encoding": "utf-16-be"}),
    ("read_lines", {"key": "{f}/pages/u32le.txt", **PAGE, "encoding": "utf-32-le", "offset": 2}),
    ("read_lines", {"key": "{f}/pages/u32be.txt", **PAGE, "encoding": "utf-32-be"}),
    ("read_lines", {"key": "{f}/pages/sig.txt", **PAGE, "encoding": "utf-8-sig"}),
    ("read_lines", {"key": "{f}/pages/sig.txt", **PAGE, "encoding": "utf-8-sig", "offset": 2}),
    ("read_lines", {"key": "{f}/pages/edge.txt", **PAGE, "offset": 2}),
    ("read_lines", {"key": "{f}/big.bin", **PAGE, "max_bytes": MAX_PAYLOAD_BYTES}),
    ("read_lines", {"key": "{f}/huge.bin", **PAGE}),
    ("read_lines", {"key": "{f}/sub", **PAGE}),
    ("read_lines", {"key": "{f}/missing", **PAGE}),
    ("read_lines", {"key": "{f}/special/locked.txt", **PAGE}),
    ("read_lines", {"key": "{f}/hard.txt", **PAGE}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "encoding": "latin-1"}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "offset": 0}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "limit": 1.5}),
    ("read_lines", {"key": "{f}/pages/mixed.txt", **PAGE, "max_bytes": MAX_PAYLOAD_BYTES + 1}),
    ("list_dir", {"key": "{f}"}),
    ("list_dir", {"key": "{f}/sub"}),
    ("list_dir", {"key": "{f}/a.txt"}),
    ("list_dir", {"key": "{f}/missing"}),
    ("ripgrep", {"key": "{f}", "mode": "files", "pattern": "*.txt", "glob": None, "context": 0}),
    ("ripgrep", {"key": "{f}/sub", "mode": "count", "pattern": "gamma", "glob": None, "context": 0}),
    ("ripgrep", {"key": "{f}/sub", "mode": "json", "pattern": "beta", "glob": None, "context": 1}),
    ("ripgrep", {"key": "{f}/sub", "mode": "count", "pattern": "gamma", "glob": "*.md", "context": 0}),
    ("ripgrep", {"key": "{f}/sub", "mode": "count", "pattern": "(", "glob": None, "context": 0}),
    ("ripgrep", {"key": "{f}/sub", "mode": "count", "pattern": "a\0", "glob": None, "context": 0}),
    ("which", {"name": "sh"}),
    ("which", {"name": "/bin/sh"}),
    ("which", {"name": "no-such-command-zz"}),
    ("which", {"name": ""}),
]

# Changes that fail the same way on both and change nothing.
SAME_FAILURES = [
    ("write", {"key": "{f}/a.txt/x", "data": b64(b"x")}),
    ("write", {"key": "{f}/a.txt/sub/x", "data": b64(b"x")}),
    # Not at the revision expected, or not there at all: a conflict, and nothing is made.
    ("write", {"key": "{f}/a.txt", "data": b64(b"x"), "expected_revision": "0:0:0:0:0"}),
    ("write", {"key": "{f}/a.txt", "data": b64(b"x"), "expected_revision": 5}),
    ("write", {"key": "{f}/missing/new.txt", "data": b64(b"x"), "expected_revision": "0:0:0:0:0"}),
    ("delete", {"key": "{f}/missing"}),
    ("delete", {"key": "{f}/sub"}),
    ("delete", {"key": "{f}"}),
]


# Commands answered alike by the app and the cloud. None depends on the shell
# (sh or bash) or on HOME.
SAME_RUN = [
    ("run", {"command": "echo out; echo err >&2; exit 3", "workdir": None, "timeout": 10}),
    ("run", {"command": "printf 'a\\000b'", "workdir": None, "timeout": 10}),
    ("run", {"command": "printf '\\377A'", "workdir": None, "timeout": 10}),
    ("run", {"command": "printf START; head -c 600000 /dev/zero | tr '\\000' x; printf END", "workdir": None, "timeout": 30}),
    ("run", {"command": "yes 中 | head -n 600000 | tr -d '\\n'", "workdir": None, "timeout": 30}),
    ("run", {"command": "sleep 5", "workdir": None, "timeout": 1}),
    ("run", {"command": "cat a.txt", "workdir": None, "timeout": 10}),
    ("run", {"command": "pwd", "workdir": None, "timeout": 10}),
    ("run", {"command": "pwd", "workdir": "sub", "timeout": 10}),
    ("run", {"command": "pwd", "workdir": "~", "timeout": 10}),
    ("run", {"command": "pwd", "workdir": "/etc", "timeout": 10}),
    ("run", {"command": "pwd", "workdir": "nope", "timeout": 10}),
    ("run", {"command": "pwd", "workdir": "a.txt", "timeout": 10}),
    # A symlink in the folder that points outside: refused on both sides.
    ("run", {"command": "pwd", "workdir": "link-out", "timeout": 10}),
    ("run", {"command": "a\0b", "workdir": None, "timeout": 10}),
    ("run", {"command": "pwd", "workdir": "a\0b", "timeout": 10}),
    ("run", {"command": "exit 0", "workdir": None, "timeout": 10}),
]

# Background processes, step by step. "{id}" in a step is the session id its case's
# first start answered. Each case has its own task id: the cloud's registry is the
# whole process's.
PROCESS_CASES = [
    [
        ("start", {"command": "printf 'one\\ntwo\\nthree\\n'; echo err >&2; (exit 3)", "workdir": None, "task_id": "cross-1",
                   "pty": False, "notify_on_complete": False, "watcher_interval": None}),
        ("wait", {"session_id": "{id}", "timeout": 10}),
        ("poll", {"session_id": "{id}"}),
        ("read_output", {"session_id": "{id}", "offset": 0, "limit": 2}),
        ("read_output", {"session_id": "{id}", "offset": 1, "limit": 1}),
        ("read_output", {"session_id": "{id}", "offset": -2, "limit": 5}),
        ("read_output", {"session_id": "{id}", "offset": 0, "limit": 200}),
        ("list_processes", {"task_id": "cross-1"}),
        ("kill", {"session_id": "{id}"}),
        ("write_stdin", {"session_id": "{id}", "data": "x"}),
        ("wait", {"session_id": "{id}", "timeout": 500}),
    ],
    [
        ("poll", {"session_id": "proc_000000000000"}),
        ("read_output", {"session_id": "proc_000000000000", "offset": 0, "limit": 200}),
        ("wait", {"session_id": "proc_000000000000", "timeout": 1}),
        ("kill", {"session_id": "proc_000000000000"}),
        ("write_stdin", {"session_id": "proc_000000000000", "data": "x"}),
    ],
    [
        ("start", {"command": "sleep 30", "workdir": None, "task_id": "cross-3", "pty": False,
                   "notify_on_complete": True, "watcher_interval": 30}),
        ("wait", {"session_id": "{id}", "timeout": 1}),
        ("wait", {"session_id": "{id}", "timeout": -1}),
        ("poll", {"session_id": "{id}"}),
        ("read_output", {"session_id": "{id}", "offset": 0, "limit": 200}),
        ("list_processes", {"task_id": "cross-3"}),
        ("kill", {"session_id": "{id}"}),
        ("poll", {"session_id": "{id}"}),
        ("wait", {"session_id": "{id}", "timeout": 5}),
        ("list_processes", {"task_id": "cross-3"}),
    ],
    [
        ("start", {"command": "head -n 1", "workdir": None, "task_id": "cross-4", "pty": False,
                   "notify_on_complete": False, "watcher_interval": None}),
        ("write_stdin", {"session_id": "{id}", "data": "hé\n"}),
        ("wait", {"session_id": "{id}", "timeout": 10}),
    ],
    [
        ("start", {"command": "printf '\\033[1mbold\\033[0m\\n'", "workdir": "sub", "task_id": "cross-5", "pty": False,
                   "notify_on_complete": False, "watcher_interval": None}),
        ("wait", {"session_id": "{id}", "timeout": 10}),
        ("read_output", {"session_id": "{id}", "offset": 0, "limit": 200}),
        ("list_processes", {"task_id": "cross-5"}),
    ],
    [
        ("start", {"command": "true", "workdir": "/etc", "task_id": "cross-6", "pty": False,
                   "notify_on_complete": False, "watcher_interval": None}),
        ("start", {"command": "true", "workdir": "nope", "task_id": "cross-6", "pty": False,
                   "notify_on_complete": False, "watcher_interval": None}),
        ("start", {"command": "true", "workdir": "a.txt", "task_id": "cross-6", "pty": False,
                   "notify_on_complete": False, "watcher_interval": None}),
        ("start", {"command": "a\0b", "workdir": None, "task_id": "cross-6", "pty": False,
                   "notify_on_complete": False, "watcher_interval": None}),
        ("list_processes", {"task_id": "cross-6"}),
    ],
]


async def play(run, steps) -> list[dict]:
    """Each step's outcome, "{id}" filled in from the case's first start."""
    session_id = ""
    outcomes = []
    for kind, args in steps:
        outcome = await run(kind, json.loads(json.dumps(args).replace("{id}", session_id)))
        if kind == "start" and "ok" in outcome and not session_id:
            session_id = outcome["ok"]["session_id"]
        outcomes.append(outcome)
    return outcomes


def normalised(value):
    """What must match: ids, pids, uptimes and start times differ by nature, so only their types are checked."""
    if isinstance(value, list):
        return [normalised(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key == "session_id" and item != "proc_000000000000":
            assert isinstance(item, str) and re.fullmatch(r"proc_[0-9a-f]{12}", item), item
            item = "<id>"
        elif key == "pid" and item is not None:
            assert type(item) is int and item >= 0, item
            item = "<pid>"
        elif key == "uptime_seconds":
            assert type(item) is int and item >= 0, item
            item = 0
        elif key == "started_at":
            assert isinstance(item, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", item), item
            item = "<time>"
        out[key] = normalised(item)
    return out


async def test_the_app_answers_as_the_cloud_does(built_client, laptop_rig, link_url, tmp_path, journal_dir):
    folder = prepare(tmp_path)
    prepared = sorted(os.listdir(folder))
    cloud = LocalWorkspaceIO(str(folder))
    app = await client(built_client, link_url, laptop_rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        for kind, template in SAME + SAME_FAILURES + SAME_RUN:
            args = fill(template, folder)
            got = await on_app(laptop_rig, kind, args)
            want = await perform(cloud, kind, args)
            assert comparable(kind, args, got) == comparable(kind, args, want), (kind, args, got, want)
        # Nothing appeared or went: no srt placeholders, no temporary files.
        assert sorted(os.listdir(folder)) == prepared
    finally:
        await app.close()


async def test_the_app_changes_files_as_the_cloud_does(built_client, laptop_rig, link_url, tmp_path, journal_dir):
    folder = prepare(tmp_path)
    app = await client(built_client, link_url, laptop_rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        assert await on_app(laptop_rig, "write", {"key": f"{folder}/new/deep/n.txt", "data": b64(b"new\n")}) == {"ok": None}
        assert (folder / "new" / "deep" / "n.txt").read_bytes() == b"new\n"
        assert os.listdir(folder / "new" / "deep") == ["n.txt"]
        assert await on_app(laptop_rig, "write", {"key": f"{folder}/run.sh", "data": b64(b"#!/bin/sh\necho\n")}) == {"ok": None}
        assert (folder / "run.sh").stat().st_mode & 0o777 == 0o755
        # On the revision its stat gave, a write lands; one too large for a frame too, once its data is whole.
        seen = (await on_app(laptop_rig, "stat", {"key": f"{folder}/sub/b.txt"}))["ok"]["revision"]
        assert await on_app(
            laptop_rig, "write", {"key": f"{folder}/sub/b.txt", "data": b64(b"changed\n"), "expected_revision": seen},
        ) == {"ok": None}
        assert (folder / "sub" / "b.txt").read_bytes() == b"changed\n"
        wio = device_io(laptop_rig.ops, laptop_rig.device_id, laptop_rig.root, folder)
        big = os.urandom(MAX_PAYLOAD_BYTES + 1)
        with pytest.raises(RevisionConflict):
            await asyncio.wait_for(wio.write(f"{folder}/big.bin", big, expected_revision="0:0:0:0:0"), 30.0)
        assert (folder / "big.bin").read_bytes() == b"x" * (MAX_PAYLOAD_BYTES + 10)
        seen = (await asyncio.wait_for(wio.stat(f"{folder}/big.bin"), 30.0)).revision
        await asyncio.wait_for(wio.write(f"{folder}/big.bin", big, expected_revision=seen), 30.0)
        assert (folder / "big.bin").read_bytes() == big
        assert await on_app(laptop_rig, "delete", {"key": f"{folder}/a.txt"}) == {"ok": None}
        assert not (folder / "a.txt").exists()
        # srt left nothing in the user's folder.
        assert sorted(os.listdir(folder)) == sorted(
            [
                ".git", "My Files ü.txt", "big.bin", "hard.txt", "huge.bin", "link-in", "link-out", "new", "pages",
                "run.sh", "special", "sub",
            ]
        )
    finally:
        await app.close()


async def test_two_writes_on_one_revision_land_once_on_the_app(built_client, laptop_rig, link_url, tmp_path, journal_dir):
    """Its file helper makes one change at a time, so the second write finds the file at another revision."""
    folder = prepare(tmp_path)
    key = f"{folder}/a.txt"
    app = await client(built_client, link_url, laptop_rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        seen = (await on_app(laptop_rig, "stat", {"key": key}))["ok"]["revision"]
        writes = [b"first\n", b"second\n"]
        outcomes = await asyncio.gather(*(
            on_app(laptop_rig, "write", {"key": key, "data": b64(data), "expected_revision": seen}) for data in writes
        ))
        [landed] = [data for data, outcome in zip(writes, outcomes) if outcome == {"ok": None}]
        assert [outcome for outcome in outcomes if outcome != {"ok": None}] == [
            {"error": {"type": "conflict", "message": CONFLICT.format(key)}},
        ]
        assert (folder / "a.txt").read_bytes() == landed
    finally:
        await app.close()


async def test_the_app_pages_text_as_the_cloud_does(built_client, laptop_rig, link_url, tmp_path, journal_dir):
    """Random text in every codec, invalid bytes and odd lengths too, paged at random by the app and the cloud."""
    folder = prepare(tmp_path)
    rng = random.Random(3)
    cases = []
    for number in range(40):
        encoding = rng.choice(list(CODE_UNITS))
        (folder / "pages" / f"random-{number}.txt").write_bytes(text(rng, encoding))
        for _ in range(4):
            cases.append({
                "key": f"{folder}/pages/random-{number}.txt", "encoding": encoding, "offset": rng.randint(1, 12),
                "limit": rng.randint(-14, 8), "max_bytes": rng.randint(0, 60),
            })
    cloud = LocalWorkspaceIO(str(folder))
    app = await client(built_client, link_url, laptop_rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        for args in cases:
            assert await on_app(laptop_rig, "read_lines", args) == await perform(cloud, "read_lines", args), args
    finally:
        await app.close()


async def test_the_app_runs_background_processes_as_the_cloud_does(
    built_client, laptop_rig, link_url, tmp_path, journal_dir, monkeypatch,
):
    folder = prepare(tmp_path)
    # The cloud starts a process with $SHELL -lic, HOME at the folder. With bash, and a
    # .hushlogin there, Ubuntu's login files add nothing to its output; no case calls
    # the exit builtin, which makes a login shell say "logout".
    monkeypatch.setenv("SHELL", "/bin/bash")
    (folder / ".hushlogin").touch()
    cloud = LocalWorkspaceIO(str(folder))
    app = await client(built_client, link_url, laptop_rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        for steps in PROCESS_CASES:
            got = await play(lambda kind, args: on_app(laptop_rig, kind, args), steps)
            want = await play(lambda kind, args: perform(cloud, kind, args), steps)
            assert normalised(got) == normalised(want), (steps, got, want)
    finally:
        await app.close()


async def test_the_app_is_stricter_where_the_laptop_must_be(built_client, laptop_rig, link_url, tmp_path, journal_dir):
    folder = prepare(tmp_path)
    outside = tmp_path / "outside" / "o.txt"
    app = await client(built_client, link_url, laptop_rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        for kind, args in [("read", {"max_bytes": None}), ("read_lines", PAGE)]:
            assert await on_app(laptop_rig, kind, {"key": f"{folder}/special/pipe", **args}) == {
                "error": {"type": "os", "code": "EINVAL", "message": f"Not a regular file: '{folder}/special/pipe'"},
            }
        assert await on_app(laptop_rig, "write", {"key": f"{folder}/hard.txt", "data": b64(b"changed")}) == {
            "error": {
                "type": "os", "code": "EMLINK",
                "message": f"File has more than one hard link, so it is not changed: '{folder}/hard.txt'",
            },
        }
        assert outside.read_text() == "outside\n"
        refusal = await on_app(laptop_rig, "check_write", {"path": ".git/config"})
        assert refusal["ok"].startswith("Write denied: '.git/config' is protected in this folder")
        assert (await on_app(laptop_rig, "write", {"key": f"{folder}/.git/config", "data": b64(b"x")}))["error"]["type"] == "sandbox"
        assert (folder / ".git" / "config").read_text() == "[core]\n"
        # A file named .git points git at another folder's config and hooks.
        listed = sorted(os.listdir(folder / "sub"))
        assert (await on_app(laptop_rig, "write", {"key": f"{folder}/sub/.git", "data": b64(b"gitdir: x\n")}))["error"]["type"] == "sandbox"
        assert not (folder / "sub" / ".git").exists()
        assert sorted(os.listdir(folder / "sub")) == listed
        for key in (str(outside), f"{folder}/link-in"):
            for kind, args in [("read", {"max_bytes": None}), ("read_lines", PAGE)]:
                assert await on_app(laptop_rig, kind, {"key": key, **args}) == {
                    "error": {"type": "sandbox", "message": f"Not a path in this folder: '{key}'"},
                }
        assert (await on_app(laptop_rig, "write", {"key": f"{folder}/x.txt", "data": "@@"}))["error"]["type"] == "value"
        # Over 1 MiB a write's data comes in a transfer: inline, it is refused before anything is asked or run.
        big = b64(b"x" * (MAX_PAYLOAD_BYTES + 1))
        assert await on_app(laptop_rig, "write", {"key": f"{folder}/x.txt", "data": big}) == {"error": {
            "type": "other",
            "message": "This write named its data in a form this computer does not take, so it was not written",
        }}
        assert not (folder / "x.txt").exists()
        home = os.environ["HOME"]
        # The command's HOME is the app's, not the folder (the toolchains find themselves through it).
        assert (await on_app(laptop_rig, "run", {"command": "echo $HOME", "workdir": None, "timeout": 10}))["ok"]["output"] == f"{home}\n"
        # A shell reports a signal as 128 + N.
        assert (await on_app(laptop_rig, "run", {"command": "kill -9 $$", "workdir": None, "timeout": 10}))["ok"]["returncode"] == 137
        # A process left in the background ends with its command, so the answer does not wait for it.
        started = asyncio.get_running_loop().time()
        answer = await on_app(laptop_rig, "run", {"command": "sleep 30 & echo started", "workdir": None, "timeout": 20})
        assert answer["ok"]["output"] == "started\n" and asyncio.get_running_loop().time() - started < 10
        # A host off the package list is refused by the sandbox's proxy.
        refused = await on_app(laptop_rig, "run", {"command": "curl -sS -o /dev/null https://example.com 2>&1", "workdir": None, "timeout": 20})
        assert "403" in refused["ok"]["output"]
        # A later command reaches a server a background process started: they share the runner's sandbox.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        started = await on_app(laptop_rig, "start", {
            "command": f"python3 -m http.server {port} --bind 127.0.0.1", "workdir": None, "task_id": "strict",
            "pty": False, "notify_on_complete": False, "watcher_interval": None,
        })
        assert started["ok"]["session_id"].startswith("proc_")
        for _ in range(50):
            fetched = await on_app(laptop_rig, "run", {"command": f"curl -sS http://127.0.0.1:{port}/a.txt", "workdir": None, "timeout": 10})
            if fetched["ok"]["output"] == "alpha\nbeta\n":
                break
            await asyncio.sleep(0.2)
        assert fetched["ok"]["output"] == "alpha\nbeta\n"
        # A real terminal, which the cloud gives only with ptyprocess installed.
        tty = await on_app(laptop_rig, "start", {
            "command": "tty", "workdir": None, "task_id": "strict", "pty": True, "notify_on_complete": False, "watcher_interval": None,
        })
        waited = await on_app(laptop_rig, "wait", {"session_id": tty["ok"]["session_id"], "timeout": 10})
        assert waited["ok"]["output"].startswith("/dev/pts/")
    finally:
        await app.close()


async def test_a_moved_folder_is_unavailable(built_client, laptop_rig, link_url, tmp_path, journal_dir):
    folder = prepare(tmp_path)
    app = await client(built_client, link_url, laptop_rig.token, journal_dir / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        assert await on_app(laptop_rig, "resolve", {"path": "a.txt"}) == {"ok": f"{folder}/a.txt"}
        folder.rename(tmp_path / "moved")
        assert (await on_app(laptop_rig, "resolve", {"path": "a.txt"}))["error"]["type"] == "folder_unavailable"
    finally:
        await app.close()
