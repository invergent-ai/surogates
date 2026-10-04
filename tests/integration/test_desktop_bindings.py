"""A chat the desktop app binds: the real server's bind operation, answered by the real app."""

from __future__ import annotations

import asyncio
import uuid
from uuid import UUID

import pytest
from sqlalchemy import select

from surogates.db.models import DeviceOperation
from surogates.devices.binding import BIND
from surogates.devices.operations import DeviceOperations, OperationRequest

from .test_desktop_file_operations import journal_dir, prepare  # noqa: F401  (fixture)
from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_device_sessions import binding, has_failed, is_bound, local_chat, send
from .test_devices import api, eventually, link_url, register  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]


def said(app, name: str) -> dict:
    return next(e for e in app.events if e["event"] == name)


async def bind_id(api, session_id: str) -> str:
    """The id of the chat's bind operation, recorded when the chat was created."""
    async with api.app.state.session_factory() as db:
        return str((await db.execute(select(DeviceOperation.id).where(
            DeviceOperation.calling_session_id == UUID(session_id), DeviceOperation.invocation_id == BIND,
        ))).scalar_one())


async def test_the_app_binds_the_chat_its_user_confirmed_and_works_in_its_folder(
    built_client, api, link_url, tmp_path, journal_dir, monkeypatch,
):
    # A server that sent op_ack before recording the outcome would let the page's
    # first message in early; slowed here, that message would be refused "still being set up".
    record = DeviceOperations.complete

    async def slow(self, *args, **kwargs):
        await asyncio.sleep(0.3)
        return await record(self, *args, **kwargs)

    monkeypatch.setattr(DeviceOperations, "complete", slow)
    folder = prepare(tmp_path)
    device = await register(api)
    app = await client(built_client, link_url, device["token"], journal_dir / "journal.sqlite", confirm=folder)
    try:
        # A refused folder says why, and the client quits before it connects.
        await app.until(lambda events: any(e["event"] in ("prepared", "error") for e in events), timeout=30.0)
        prepared = next(e for e in app.events if e["event"] in ("prepared", "error"))
        assert prepared["event"] == "prepared", prepared
        assert prepared["folder"] == str(folder)
        # The sheet its user accepted names the one file prepare() hard-links from outside the folder.
        assert said(app, "sheet")["links"] == {"count": 1, "examples": ["hard.txt"], "complete": True}
        await app.until(connected, timeout=30.0)

        # A nonce the app never gave out: nobody confirmed a folder for this chat.
        stranger = await local_chat(api, device["id"], folder=prepared["folder"], nonce="z" * 32)
        await eventually(lambda: has_failed(api, stranger), timeout=10.0)
        assert (await binding(api, stranger)).message == "This folder was not confirmed on this computer"

        session_id = await local_chat(api, device["id"], folder=prepared["folder"], nonce=prepared["nonce"])
        bind = await bind_id(api, session_id)
        # The page sends its first message as soon as bindSession resolves: on the bind's op_ack.
        await app.until(lambda events: {"event": "ack", "id": bind} in events, timeout=10.0)
        assert (await send(api, session_id)).status_code == 202
        assert await is_bound(api, session_id)
        ops = DeviceOperations(api.app.state.session_factory, api.app.state.redis)
        resolved = await asyncio.wait_for(ops.run(OperationRequest(
            device_id=UUID(device["id"]), root_session_id=UUID(session_id), calling_session_id=UUID(session_id),
            invocation_id=f"call-{uuid.uuid4()}", ordinal=1, kind="resolve", args={"path": "a.txt"},
        )), 30.0)
        assert resolved == {"ok": f"{folder}/a.txt"}
    finally:
        await app.close()


async def test_the_echo_client_refuses_a_folder_for_every_chat_beside_a_confirmed_one(built_client, tmp_path):
    process = await asyncio.create_subprocess_exec(
        "node", str(built_client), "--url", "ws://127.0.0.1:1", "--token", "t", "--journal", str(tmp_path / "j.sqlite"),
        "--folder", str(tmp_path), "--confirm", str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), 30.0)
    assert process.returncode == 2
    assert out == b""
    assert b"usage: echo-client" in err
