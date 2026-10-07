"""What the user of the computer denies, as the cloud's own tools read it: the real
handlers, over the real link, answered by the real app in a chat that asks every time."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import pytest

from surogates.devices.operations import DeviceOperations
from surogates.tools.builtin import file_ops, terminal
from surogates.tools.utils import process_registry

from .test_desktop_bindings import bind_id
from .test_desktop_file_operations import VM, built_agent_disk, journal_dir  # noqa: F401  (fixtures)
from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_device_sessions import local_chat
from .test_devices import api, device_io, link_url, register  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]

# The app's user denies whatever names this word, and allows the rest.
WORD = "denied"


@VM
async def test_what_its_user_denies_reaches_each_tool_as_not_done(built_client, built_agent_disk, api, link_url, tmp_path, journal_dir):
    folder = (tmp_path / "folder").resolve()
    folder.mkdir()
    device = await register(api)
    app = await client(built_client, link_url, device["token"], journal_dir / "journal.sqlite", confirm=folder, ask=WORD)
    try:
        await app.until(lambda events: any(e["event"] in ("prepared", "error") for e in events), timeout=30.0)
        prepared = next(e for e in app.events if e["event"] in ("prepared", "error"))
        assert prepared["event"] == "prepared", prepared
        await app.until(connected, timeout=30.0)
        session_id = await local_chat(api, device["id"], folder=prepared["folder"], nonce=prepared["nonce"])
        bind = await bind_id(api, session_id)
        await app.until(lambda events: {"event": "ack", "id": bind} in events, timeout=10.0)
        ops = DeviceOperations(api.app.state.session_factory, api.app.state.redis)

        async def call(handler, args: dict) -> dict:
            """One tool call, on the chat's folder through the app."""
            wio = device_io(ops, UUID(device["id"]), UUID(session_id), folder)
            return json.loads(await asyncio.wait_for(handler(args, workspace_io=wio, task_id=session_id), 60.0))

        # A denied command is blocked and never ran, in the foreground or the background; an allowed one runs.
        blocked = {"output": "", "exit_code": -1, "error": "The user denied this command on this computer", "status": "blocked"}
        assert await call(terminal._terminal_handler, {"command": f"touch {WORD}.txt"}) == blocked
        assert await call(terminal._terminal_handler, {"command": f"echo {WORD}", "background": True}) == blocked
        assert (await call(terminal._terminal_handler, {"command": "echo allowed"}))["output"] == "allowed"

        # A denied write is a PermissionError, which write_file reports as expected.
        assert await call(file_ops._write_file_handler, {"path": f"{WORD}.txt", "content": "x"}) == {
            "error": "[Errno 13] The user denied this change on this computer",
        }
        assert not (folder / f"{WORD}.txt").exists()

        # In a patch, each denied file fails alone; the next is asked about on its own, and applied.
        # The blank line keeps the Delete: the V4A parser drops one with no body.
        (folder / f"{WORD}-old.md").write_text("old")
        patch = (
            f"*** Begin Patch\n*** Add File: {WORD}.md\n+no\n*** Delete File: {WORD}-old.md\n\n"
            "*** Add File: kept.md\n+yes\n*** End Patch"
        )
        patched = await call(file_ops._patch_handler, {"mode": "patch", "patch": patch})
        assert patched["status"] == "partial"
        assert patched["files"][0] == {
            "path": f"{WORD}.md", "error": "Failed to create: [Errno 13] The user denied this change on this computer",
        }
        assert patched["files"][1] == {
            "path": f"{WORD}-old.md", "error": "Failed to delete: [Errno 13] The user denied this change on this computer",
        }
        assert patched["files"][2]["status"] == "ok"
        assert not (folder / f"{WORD}.md").exists()
        assert (folder / f"{WORD}-old.md").read_text() == "old"
        assert (folder / "kept.md").read_text() == "yes"

        # Denied input to a process is a write that failed, as the process tool shapes one.
        started = await call(terminal._terminal_handler, {"command": "cat", "background": True})
        handle = started["session_id"]
        assert await call(process_registry._handle_process, {"action": "write", "session_id": handle, "data": f"{WORD}\n"}) == {
            "status": "error", "error": "The user denied this input on this computer",
        }
        assert (await call(process_registry._handle_process, {"action": "kill", "session_id": handle}))["status"] == "killed"

        # What the user was asked, in order: nothing that only reads, nor the kill.
        asked = [
            (e["kind"], e.get("command") or e.get("path") or e.get("data"))
            for e in app.events if e["event"] == "approval"
        ]
        assert asked == [
            ("command", f"touch {WORD}.txt"),
            ("command", f"echo {WORD}"),
            ("command", "echo allowed"),
            ("change", str(folder / f"{WORD}.txt")),
            ("change", str(folder / f"{WORD}.md")),
            ("change", str(folder / f"{WORD}-old.md")),
            ("change", str(folder / "kept.md")),
            ("command", "cat"),
            ("input", f"{WORD}\n"),
        ]
    finally:
        await app.close()
