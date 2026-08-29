"""Blind-overwrite refusal, and the read record surviving the executor fork.

The sandbox executor forks a child per tool call and only pipes the
result back, so a read recorded inside a handler is discarded with the
child that recorded it. These tests pin both halves of the fix: the
handler refuses an unread overwrite, and the parent daemon carries the
read record across the fork boundary.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from surogates.sandbox import executor_server
from surogates.tools.builtin import file_ops


@pytest.fixture(autouse=True)
def _clean_trackers():
    file_ops._read_tracker.clear()
    executor_server._READ_TIMESTAMPS.clear()
    yield
    file_ops._read_tracker.clear()
    executor_server._READ_TIMESTAMPS.clear()


async def _write(path: str, content: str = "new") -> dict:
    return json.loads(
        await file_ops._write_file_handler({"path": path, "content": content})
    )


async def test_new_file_writes_without_a_prior_read(tmp_path):
    target = str(tmp_path / "fresh.txt")
    assert (await _write(target))["status"] == "ok"
    assert open(target).read() == "new"


async def test_unread_overwrite_is_refused_and_file_untouched(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("original")

    result = await _write(str(target))

    assert "Refusing to overwrite" in result["error"]
    assert "read_file" in result["error"]
    assert target.read_text() == "original", "refused write must not touch the file"


async def test_overwrite_allowed_after_the_read_is_seeded(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("original")
    resolved = str(target.resolve())

    file_ops.seed_read_timestamps({resolved: os.path.getmtime(resolved)})

    assert (await _write(str(target)))["status"] == "ok"
    assert target.read_text() == "new"


async def test_seeded_record_answers_has_read(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    resolved = str(target.resolve())

    assert not file_ops.has_read(resolved)
    file_ops.seed_read_timestamps({resolved: os.path.getmtime(resolved)})
    assert file_ops.has_read(resolved)


# --- the parent-side record that survives the fork -------------------------


def test_successful_read_is_recorded_by_the_parent(tmp_path):
    target = tmp_path / "seen.txt"
    target.write_text("x")

    executor_server._record_read(
        "read_file", {"path": "seen.txt"}, str(tmp_path), json.dumps({"content": "x"}),
    )

    assert str(target.resolve()) in executor_server._READ_TIMESTAMPS


def test_failed_read_records_nothing(tmp_path):
    target = tmp_path / "seen.txt"
    target.write_text("x")

    executor_server._record_read(
        "read_file", {"path": "seen.txt"}, str(tmp_path), json.dumps({"error": "nope"}),
    )

    assert executor_server._READ_TIMESTAMPS == {}, (
        "a refused write must not authorise its own retry"
    )


def test_non_file_tool_records_nothing(tmp_path):
    executor_server._record_read(
        "terminal", {"path": "x"}, str(tmp_path), json.dumps({"exit_code": 0}),
    )
    assert executor_server._READ_TIMESTAMPS == {}


def test_record_is_capped(tmp_path):
    for i in range(executor_server._MAX_READ_TIMESTAMPS + 2):
        f = tmp_path / f"f{i}.txt"
        f.write_text("x")
        executor_server._record_read(
            "read_file", {"path": f.name}, str(tmp_path), json.dumps({"content": "x"}),
        )
    assert len(executor_server._READ_TIMESTAMPS) <= executor_server._MAX_READ_TIMESTAMPS


async def test_record_crosses_the_fork_into_the_handler(tmp_path):
    """The end-to-end path: parent records a read, child sees it."""
    target = tmp_path / "existing.txt"
    target.write_text("original")

    # Parent notes the read (as execute_in_child does after a successful call).
    executor_server._record_read(
        "read_file", {"path": "existing.txt"}, str(tmp_path),
        json.dumps({"content": "original"}),
    )
    # Child is handed a copy of the parent's map and seeds its own tracker.
    file_ops.seed_read_timestamps(dict(executor_server._READ_TIMESTAMPS))

    assert (await _write(str(target)))["status"] == "ok"


# --- across a REAL fork, through the daemon's HTTP layer -------------------
#
# The tests above simulate the parent/child boundary. These cross it: each
# /execute genuinely forks, so they fail if the record does not actually
# survive process creation -- which is the bug being fixed.


def _daemon(workspace, tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("geesefs /workspace fuse.geesefs rw 0 0\n")
    executor_server.init_registry()
    return executor_server.create_app(
        token="t", workspace=str(workspace), mounts_path=str(mounts),
        require_fuse=False,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://daemon",
    )


async def _execute(client, name, args):
    resp = await client.post(
        "/execute",
        json={"name": name, "args": args, "timeout": 60},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200, resp.text
    return json.loads(resp.text)


async def test_daemon_refuses_unread_overwrite_across_forks(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "conf.ini").write_text("original")
    app = _daemon(ws, tmp_path)

    async with _client(app) as client:
        out = await _execute(client, "write_file", {
            "path": str(ws / "conf.ini"), "content": "clobbered"})

    assert "Refusing to overwrite" in json.dumps(out)
    assert (ws / "conf.ini").read_text() == "original"


async def test_daemon_allows_overwrite_after_a_real_read(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "conf.ini").write_text("original")
    app = _daemon(ws, tmp_path)

    async with _client(app) as client:
        # Fork #1: the read. Its tracker entry dies with the child -- only
        # the parent's record carries it forward.
        await _execute(client, "read_file", {"path": str(ws / "conf.ini")})
        # Fork #2: the write, in a process that never saw the read.
        out = await _execute(client, "write_file", {
            "path": str(ws / "conf.ini"), "content": "updated"})

    assert "Refusing to overwrite" not in json.dumps(out), out
    assert (ws / "conf.ini").read_text() == "updated"


async def test_daemon_still_creates_new_files_across_forks(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    app = _daemon(ws, tmp_path)

    async with _client(app) as client:
        out = await _execute(client, "write_file", {
            "path": str(ws / "brand-new.txt"), "content": "hello"})

    assert "Refusing to overwrite" not in json.dumps(out), out
    assert (ws / "brand-new.txt").read_text() == "hello"
