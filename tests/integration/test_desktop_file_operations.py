"""The desktop app's file operations against the cloud's: the same answers from both.

Each case runs on the real app, over the real device link and inside its
sandbox, and on the Python reference laptop (``tests/fake_laptop.perform`` over
``LocalWorkspaceIO``), against one folder. Where the laptop is stricter by
design, the case says so and asserts the laptop's own answer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

from surogates.devices.operations import OperationRequest
from surogates.devices.workspace import MAX_PAYLOAD_BYTES, TOO_LARGE
from surogates.tools.workspace_io.local import LocalWorkspaceIO
from tests.fake_laptop import perform

from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    api,
    laptop_rig,
    link_url,
    request_for,
)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]


def prepare(base: Path) -> Path:
    folder = base / "folder"
    (folder / "sub" / "deep").mkdir(parents=True)
    (folder / "special").mkdir()
    (folder / "a.txt").write_text("alpha\nbeta\n")
    (folder / "sub" / "b.txt").write_text("beta gamma\n")
    (folder / "sub" / "deep" / "c.md").write_text("gamma\n")
    (folder / "My Files ü.txt").write_text("unicode\n")
    (folder / "big.bin").write_bytes(b"x" * (MAX_PAYLOAD_BYTES + 10))
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
    return folder.resolve()


def fill(value, folder: Path):
    if isinstance(value, str):
        return value.replace("{f}", str(folder))
    if isinstance(value, dict):
        return {key: fill(item, folder) for key, item in value.items()}
    return value


def comparable(kind: str, args: dict, outcome: dict) -> dict:
    """What must match: listings and searches in any order, rg's timings and wording aside."""
    if "error" in outcome:
        error = outcome["error"]
        if error.get("type") == "ripgrep" and error["message"].startswith("rg exited"):
            return {"error": {"type": "ripgrep", "message": error["message"].split(":")[0]}}
        return outcome
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
    ("read", {"key": "{f}/a.txt", "max_bytes": None}),
    ("read", {"key": "{f}/a.txt", "max_bytes": 3}),
    ("read", {"key": "{f}/a.txt", "max_bytes": 0}),
    ("read", {"key": "{f}/big.bin", "max_bytes": None}),
    ("read", {"key": "{f}/big.bin", "max_bytes": 8192}),
    ("read", {"key": "{f}/sub", "max_bytes": None}),
    ("read", {"key": "{f}/missing", "max_bytes": None}),
    ("read", {"key": "{f}/special/locked.txt", "max_bytes": None}),
    ("read", {"key": "{f}/hard.txt", "max_bytes": None}),
    ("read", {"key": "{f}/My Files ü.txt", "max_bytes": None}),
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
    ("delete", {"key": "{f}/missing"}),
    ("delete", {"key": "{f}/sub"}),
    ("delete", {"key": "{f}"}),
]


async def test_the_app_answers_as_the_cloud_does(built_client, laptop_rig, link_url, tmp_path):
    folder = prepare(tmp_path)
    prepared = sorted(os.listdir(folder))
    cloud = LocalWorkspaceIO(str(folder))
    app = await client(built_client, link_url, laptop_rig.token, tmp_path / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        for kind, template in SAME + SAME_FAILURES:
            args = fill(template, folder)
            got = await on_app(laptop_rig, kind, args)
            want = await perform(cloud, kind, args)
            assert comparable(kind, args, got) == comparable(kind, args, want), (kind, args, got, want)
        # Nothing appeared or went: no srt placeholders, no temporary files.
        assert sorted(os.listdir(folder)) == prepared
    finally:
        await app.close()


async def test_the_app_changes_files_as_the_cloud_does(built_client, laptop_rig, link_url, tmp_path):
    folder = prepare(tmp_path)
    app = await client(built_client, link_url, laptop_rig.token, tmp_path / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        assert await on_app(laptop_rig, "write", {"key": f"{folder}/new/deep/n.txt", "data": b64(b"new\n")}) == {"ok": None}
        assert (folder / "new" / "deep" / "n.txt").read_bytes() == b"new\n"
        assert os.listdir(folder / "new" / "deep") == ["n.txt"]
        assert await on_app(laptop_rig, "write", {"key": f"{folder}/run.sh", "data": b64(b"#!/bin/sh\necho\n")}) == {"ok": None}
        assert (folder / "run.sh").stat().st_mode & 0o777 == 0o755
        assert await on_app(laptop_rig, "delete", {"key": f"{folder}/a.txt"}) == {"ok": None}
        assert not (folder / "a.txt").exists()
        # srt left nothing in the user's folder.
        assert sorted(os.listdir(folder)) == sorted(
            [".git", "My Files ü.txt", "big.bin", "hard.txt", "link-in", "link-out", "new", "run.sh", "special", "sub"]
        )
    finally:
        await app.close()


async def test_the_app_is_stricter_where_the_laptop_must_be(built_client, laptop_rig, link_url, tmp_path):
    folder = prepare(tmp_path)
    outside = tmp_path / "outside" / "o.txt"
    app = await client(built_client, link_url, laptop_rig.token, tmp_path / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        assert await on_app(laptop_rig, "read", {"key": f"{folder}/special/pipe", "max_bytes": None}) == {
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
        for key in (str(outside), f"{folder}/link-in"):
            assert await on_app(laptop_rig, "read", {"key": key, "max_bytes": None}) == {
                "error": {"type": "sandbox", "message": f"Not a path in this folder: '{key}'"},
            }
        assert (await on_app(laptop_rig, "write", {"key": f"{folder}/x.txt", "data": "@@"}))["error"]["type"] == "value"
        big = b64(b"x" * (MAX_PAYLOAD_BYTES + 1))
        assert await on_app(laptop_rig, "write", {"key": f"{folder}/x.txt", "data": big}) == {
            "error": {"type": "os", "code": "EFBIG", "message": TOO_LARGE},
        }
        assert (await on_app(laptop_rig, "run", {"command": "true", "workdir": None, "timeout": 5}))["error"]["type"] == "unsupported"
    finally:
        await app.close()


async def test_a_moved_folder_is_unavailable(built_client, laptop_rig, link_url, tmp_path):
    folder = prepare(tmp_path)
    app = await client(built_client, link_url, laptop_rig.token, tmp_path / "journal.sqlite", folder=folder)
    try:
        await app.until(connected)
        assert await on_app(laptop_rig, "resolve", {"path": "a.txt"}) == {"ok": f"{folder}/a.txt"}
        folder.rename(tmp_path / "moved")
        assert (await on_app(laptop_rig, "resolve", {"path": "a.txt"}))["error"]["type"] == "folder_unavailable"
    finally:
        await app.close()
