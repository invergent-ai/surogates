"""A command's connections to hosts off the package list, through the cloud's own
terminal tool and the real app, its commands in the VM: this computer's
own services are refused without asking, any other host is asked about, and each
refusal reaches the agent as the proxy's 403 and the app's notice; a host its user
allows goes through."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest

from surogates.devices.operations import DeviceOperations
from surogates.tools.builtin import terminal

from .test_desktop_bindings import bind_id
from .test_desktop_file_operations import VM, built_agent_disk, journal_dir  # noqa: F401  (fixtures)
from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_device_sessions import local_chat
from .test_devices import api, device_io, link_url, register  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]

# TEST-NET-1 (RFC 5737): addresses no host answers. Refused, the proxy answers 403 before
# anything is dialed; let through, curl gives up at its --max-time (status 000, exit 28),
# or the proxy answers 502 at once on a computer with no route.
AWAY = "192.0.2.1"
NEXT_DOOR = "192.0.2.2"


def status(url: str) -> str:
    """A command that prints the HTTP status it gets. --noproxy '' sends loopback to the proxy, past NO_PROXY."""
    return f"curl -sS --max-time 5 --noproxy '' -o /dev/null -w '%{{http_code}}\\n' {url} 2>/dev/null"


async def chat_on(app, api, device, folder: Path):
    """A chat bound to *folder*, which the app confirmed, as the page binds one: its
    session id, the folder as the app says it, and a call of the cloud's terminal tool in it."""
    await app.until(lambda events: any(e["event"] in ("prepared", "error") for e in events), timeout=30.0)
    prepared = next(e for e in app.events if e["event"] in ("prepared", "error"))
    assert prepared["event"] == "prepared", prepared
    await app.until(connected, timeout=30.0)
    session_id = await local_chat(api, device["id"], folder=prepared["folder"], nonce=prepared["nonce"])
    bind = await bind_id(api, session_id)
    await app.until(lambda events: {"event": "ack", "id": bind} in events, timeout=10.0)
    ops = DeviceOperations(api.app.state.session_factory, api.app.state.redis)

    async def terminal_call(command: str) -> dict:
        """One tool call through the cloud's terminal handler, with a workspace of its own
        as the worker gives each, on the chat's folder through the app."""
        wio = device_io(ops, UUID(device["id"]), UUID(session_id), folder)
        raw = await asyncio.wait_for(terminal._terminal_handler({"command": command}, workspace_io=wio, task_id=session_id), 60.0)
        return json.loads(raw)

    return session_id, prepared["folder"], terminal_call


def network_prompt(session_id: str, folder: str, host: str) -> dict:
    """The approval event the app says for a network prompt of the chat, about *host* on port 9."""
    return {
        "event": "approval", "kind": "network",
        "chat": {"agent": "the cross-check", "root": session_id, "calling": session_id, "folder": folder},
        "host": host, "port": 9, "privateNetwork": False,
    }


@VM
async def test_refusals_reach_the_agent_through_the_terminal_tool(built_client, built_agent_disk, api, link_url, tmp_path, journal_dir):
    folder = (tmp_path / "folder").resolve()
    folder.mkdir()
    device = await register(api)
    # Without --ask the chat works freely: only the network asks, and the echo client denies it.
    app = await client(built_client, link_url, device["token"], journal_dir / "journal.sqlite", confirm=folder)
    try:
        session_id, bound, terminal_call = await chat_on(app, api, device, folder)
        # The root's own name, which only the guest gives a command: this ran in the VM.
        named = await terminal_call("hostname")
        assert named["output"] == "surogate", named
        own = await terminal_call(status("http://127.0.0.1:9/"))
        assert own["output"] == "403\n\nThis computer does not let a chat reach its own network services (127.0.0.1:9)"
        away = await terminal_call(status(f"http://{AWAY}:9/"))
        assert away["output"] == f"403\n\nThis computer did not allow network access to {AWAY}:9."
        assert [own["exit_code"], away["exit_code"]] == [0, 0]
        # Only the second was asked about.
        assert [e for e in app.events if e["event"] == "approval"] == [network_prompt(session_id, bound, AWAY)]
    finally:
        await app.close()


@VM
async def test_a_host_its_user_allows_goes_through_the_terminal_tool(built_client, built_agent_disk, api, link_url, tmp_path, journal_dir):
    folder = (tmp_path / "folder").resolve()
    folder.mkdir()
    device = await register(api)
    # With --ask the chat asks every time, and the echo client allows what does not name the word.
    app = await client(built_client, link_url, device["token"], journal_dir / "journal.sqlite", confirm=folder, ask="denied")
    try:
        session_id, bound, terminal_call = await chat_on(app, api, device, folder)
        allowed = await terminal_call(status(f"http://{NEXT_DOOR}:9/"))
        # Let through, with no notice.
        assert (allowed["output"], allowed["exit_code"]) in (("000", 28), ("502", 0)), allowed
        # The command is asked about, then its connection, once.
        approvals = [e for e in app.events if e["event"] == "approval"]
        assert [e["kind"] for e in approvals] == ["command", "network"]
        assert approvals[1] == network_prompt(session_id, bound, NEXT_DOOR)
    finally:
        await app.close()
