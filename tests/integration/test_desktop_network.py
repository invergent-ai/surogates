"""A command's connections to hosts off the package list, through the cloud's own
terminal tool and the real app: this computer's own services are refused without
asking, any other host is asked about, and each refusal reaches the agent as srt's
403 and the app's notice."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import pytest

from surogates.devices.operations import DeviceOperations
from surogates.tools.builtin import terminal

from .test_desktop_bindings import bind_id
from .test_desktop_file_operations import journal_dir  # noqa: F401  (fixture)
from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_device_sessions import local_chat
from .test_devices import api, device_io, link_url, register  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]

# TEST-NET-1 (RFC 5737): an address no host answers, refused here before anything is dialed.
AWAY = "192.0.2.1"


def status(url: str) -> str:
    """A command that prints the HTTP status it gets. --noproxy '' sends loopback to srt's proxy, past srt's NO_PROXY."""
    return f"curl -sS --max-time 5 --noproxy '' -o /dev/null -w '%{{http_code}}\\n' {url} 2>/dev/null"


async def test_refusals_reach_the_agent_through_the_terminal_tool(built_client, api, link_url, tmp_path, journal_dir):
    folder = (tmp_path / "folder").resolve()
    folder.mkdir()
    device = await register(api)
    # Without --ask the chat works freely: only the network asks, and the echo client denies it.
    app = await client(built_client, link_url, device["token"], journal_dir / "journal.sqlite", confirm=folder)
    try:
        await app.until(lambda events: any(e["event"] in ("prepared", "error") for e in events), timeout=30.0)
        prepared = next(e for e in app.events if e["event"] in ("prepared", "error"))
        assert prepared["event"] == "prepared", prepared
        await app.until(connected, timeout=30.0)
        session_id = await local_chat(api, device["id"], folder=prepared["folder"], nonce=prepared["nonce"])
        bind = await bind_id(api, session_id)
        await app.until(lambda events: {"event": "ack", "id": bind} in events, timeout=10.0)
        ops = DeviceOperations(api.app.state.session_factory, api.app.state.redis)
        wio = device_io(ops, UUID(device["id"]), UUID(session_id), folder)

        async def terminal_call(command: str) -> dict:
            """One tool call through the cloud's terminal handler, on the chat's folder through the app."""
            raw = await asyncio.wait_for(terminal._terminal_handler({"command": command}, workspace_io=wio, task_id=session_id), 60.0)
            return json.loads(raw)

        own = await terminal_call(status("http://127.0.0.1:9/"))
        assert own["output"] == "403\n\nThis computer does not let a chat reach its own network services (127.0.0.1:9)"
        away = await terminal_call(status(f"http://{AWAY}:9/"))
        assert away["output"] == f"403\n\nThis computer did not allow network access to {AWAY}:9."
        assert [own["exit_code"], away["exit_code"]] == [0, 0]
        # Only the second was asked about.
        assert [e for e in app.events if e["event"] == "approval"] == [{
            "event": "approval", "kind": "network",
            "chat": {"agent": "the cross-check", "root": session_id, "calling": session_id, "folder": prepared["folder"]},
            "host": AWAY, "port": 9, "privateNetwork": False,
        }]
    finally:
        await app.close()
