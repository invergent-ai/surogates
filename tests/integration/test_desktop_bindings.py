"""A chat the desktop app binds: the real server's bind operation, answered by the real app."""

from __future__ import annotations

import asyncio
import uuid
from uuid import UUID

import pytest

from surogates.devices.operations import DeviceOperations, OperationRequest

from .test_desktop_file_operations import journal_dir, prepare  # noqa: F401  (fixture)
from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_device_sessions import binding, has_failed, is_bound, local_chat, send
from .test_devices import api, eventually, link_url, register  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]


def said(app, name: str) -> dict:
    return next(e for e in app.events if e["event"] == name)


async def test_the_app_binds_the_chat_its_user_confirmed_and_works_in_its_folder(
    built_client, api, link_url, tmp_path, journal_dir,
):
    folder = prepare(tmp_path)
    device = await register(api)
    app = await client(built_client, link_url, device["token"], journal_dir / "journal.sqlite", confirm=folder)
    try:
        await app.until(connected, timeout=30.0)
        # The sheet its user accepted names the one file prepare() hard-links from outside the folder.
        assert said(app, "sheet")["links"] == {"count": 1, "examples": ["hard.txt"], "complete": True}
        prepared = said(app, "prepared")
        assert prepared["folder"] == str(folder)

        # A nonce the app never gave out: nobody confirmed a folder for this chat.
        stranger = await local_chat(api, device["id"], folder=prepared["folder"], nonce="z" * 32)
        await eventually(lambda: has_failed(api, stranger), timeout=10.0)
        assert (await binding(api, stranger)).message == "This folder was not confirmed on this computer"

        acks = sum(e["event"] == "ack" for e in app.events)
        session_id = await local_chat(api, device["id"], folder=prepared["folder"], nonce=prepared["nonce"])
        # The page sends its first message as soon as bindSession resolves: on the bind's op_ack.
        await app.until(lambda events: sum(e["event"] == "ack" for e in events) > acks, timeout=10.0)
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
