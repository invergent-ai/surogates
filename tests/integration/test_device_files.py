"""A local-folder chat's file panel: what it shows and opens is in the folder on the user's computer."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio

import surogates.api.routes.workspace as workspace_routes
from surogates.devices.workspace import MAX_WALK_FILES
from surogates.session.provisioning import create_child_session
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import FakeLaptop

from .conftest import issue_service_account_token
from .test_device_sessions import is_bound, local_chat
from .test_devices import (  # noqa: F401  (fixtures)
    NONCE,
    FOLDER,
    add_user,
    api,
    eventually,
    link_url,
    register,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def chat(api, link_url, tmp_path):
    """A chat bound to a folder of a connected computer: its id, the folder, and the laptop."""
    device = await register(api)
    folder = (tmp_path / "laptop").resolve()
    folder.mkdir()
    laptop = FakeLaptop(link_url, device["token"], LocalWorkspaceIO(str(folder)))
    laptop.prepare(NONCE, FOLDER)
    session_id = await local_chat(api, device["id"])
    await laptop.connect()
    await eventually(lambda: is_bound(api, session_id))
    yield SimpleNamespace(id=session_id, folder=folder, laptop=laptop, device_id=UUID(device["id"]))
    await laptop.disconnect()


def url(chat, route: str, session_id: str | None = None) -> str:
    return f"/v1/sessions/{session_id or chat.id}/workspace/{route}"


async def another_member(api) -> str:
    """A token of another user of the chat's org."""
    _user_id, token = await add_user(api.app.state.session_factory, api.org_id)
    return token


async def test_the_tree_is_the_folders(api, chat):
    (chat.folder / "notes").mkdir()
    (chat.folder / "notes" / "a.md").write_text("alpha")
    (chat.folder / "node_modules" / "x").mkdir(parents=True)
    (chat.folder / "node_modules" / "x" / "i.js").write_text("")
    (chat.folder / "_whiteboard").mkdir()
    (chat.folder / "_whiteboard" / "canvas.json").write_text("{}")
    response = await api.client.get(url(chat, "tree"), headers=api.auth())
    assert response.status_code == 200, response.text
    assert response.json()["entries"] == [{
        "name": "notes", "path": "notes", "kind": "dir", "size": None,
        "children": [{"name": "a.md", "path": "notes/a.md", "kind": "file", "size": None, "children": None}],
    }]
    assert "walk" in chat.laptop.ran


async def test_a_folder_the_tree_hides_uses_none_of_the_walks_cap(api, chat):
    (chat.folder / ".cache").mkdir()
    for name in range(MAX_WALK_FILES + 1):
        (chat.folder / ".cache" / str(name)).touch()
    for n in range(12):
        (chat.folder / f"src{n}").mkdir()
        (chat.folder / f"src{n}" / "main.py").write_text("x")
    response = await api.client.get(url(chat, "tree"), headers=api.auth())
    assert response.status_code == 200, response.text
    assert sorted(entry["name"] for entry in response.json()["entries"]) == sorted(f"src{n}" for n in range(12))
    assert response.json()["truncated"] is False


async def test_a_file_is_opened_from_the_folder(api, chat):
    (chat.folder / "a.md").write_text("alpha")
    (chat.folder / "dot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    text = await api.client.get(url(chat, "file"), params={"path": "a.md"}, headers=api.auth())
    assert text.status_code == 200, text.text
    assert (text.json()["content"], text.json()["size"]) == ("alpha", 5)
    image = await api.client.get(url(chat, "file"), params={"path": "dot.png"}, headers=api.auth())
    assert image.status_code == 200, image.text
    assert base64.b64decode(image.json()["content"]) == b"\x89PNG\r\n\x1a\n"
    missing = await api.client.get(url(chat, "file"), params={"path": "missing.md"}, headers=api.auth())
    assert missing.status_code == 404, missing.text


async def test_a_path_the_computer_cannot_take_is_the_users_error(api, chat):
    response = await api.client.get(url(chat, "file"), params={"path": "a\x00b.md"}, headers=api.auth())
    assert response.status_code == 400, response.text


async def test_a_download_is_the_files_bytes(api, chat):
    (chat.folder / "report.bin").write_bytes(b"\x00\x01bytes")
    response = await api.client.get(url(chat, "download"), params={"path": "report.bin"}, headers=api.auth())
    assert response.status_code == 200, response.text
    assert response.content == b"\x00\x01bytes"


async def test_a_file_over_one_frame_crosses_whole(api, chat):
    data = bytes(range(256)) * 8192  # 2 MiB: a transfer, not one frame
    (chat.folder / "big.bin").write_bytes(data)
    response = await api.client.get(url(chat, "download"), params={"path": "big.bin"}, headers=api.auth())
    assert response.status_code == 200, response.text
    assert response.content == data


async def test_a_paused_chats_files_are_still_shown(api, chat):
    (chat.folder / "a.md").write_text("alpha")
    paused = await api.client.post(f"/v1/sessions/{chat.id}/pause", headers=api.auth())
    assert paused.status_code == 200, paused.text
    response = await api.client.get(url(chat, "file"), params={"path": "a.md"}, headers=api.auth())
    assert response.status_code == 200, response.text


async def test_an_offline_computer_is_said_at_once(api, chat):
    await chat.laptop.disconnect()

    async def offline() -> bool:
        response = await api.client.get(url(chat, "tree"), headers=api.auth())
        return response.status_code == 503

    await eventually(offline, timeout=10.0)
    response = await api.client.get(url(chat, "file"), params={"path": "a.md"}, headers=api.auth())
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error": "device_offline", "message": "The files are on Flavius's ThinkPad, which is offline",
    }


async def test_a_revoked_computer_is_said(api, chat):
    revoked = await api.client.delete(f"/v1/devices/{chat.device_id}", headers=api.auth())
    assert revoked.status_code == 204, revoked.text
    response = await api.client.get(url(chat, "tree"), headers=api.auth())
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "device_revoked"


async def test_a_read_its_computer_does_not_answer_in_time_is_cancelled(api, chat, monkeypatch):
    monkeypatch.setattr(workspace_routes, "READ_WITHIN_S", 0.5)
    chat.laptop.hold = True
    response = await api.client.get(url(chat, "tree"), headers=api.auth())
    assert response.status_code == 504
    assert response.json()["detail"]["error"] == "device_timeout"

    async def told_to_stop() -> bool:
        return len(chat.laptop.cancelled) == 1

    await eventually(told_to_stop)


async def test_a_chat_whose_folder_is_still_being_set_up_shows_nothing_yet(api):
    device = await register(api)
    session_id = await local_chat(api, device["id"])
    response = await api.client.get(f"/v1/sessions/{session_id}/workspace/tree", headers=api.auth())
    assert response.status_code == 409, response.text


async def test_another_member_of_the_org_reaches_nothing_on_the_users_computer(api, chat):
    stranger = await another_member(api)
    (chat.folder / "a.md").write_text("alpha")
    store = api.app.state.session_store
    child = await create_child_session(store=store, parent=await store.get_session(UUID(chat.id)), channel="worker")
    ran = list(chat.laptop.ran)
    for session_id in (chat.id, str(child.id)):
        for route, params in (("tree", {}), ("file", {"path": "a.md"}), ("download", {"path": "a.md"})):
            response = await api.client.get(url(chat, route, session_id), params=params, headers=api.auth(stranger))
            assert response.status_code == 404, (session_id, route, response.text)
    message = await api.client.post(
        f"/v1/sessions/{chat.id}/messages", json={"content": "Read my notes"}, headers=api.auth(stranger),
    )
    assert message.status_code == 404, message.text
    assert chat.laptop.ran == ran
    # The chat's own user reaches its sub-agent's files: the root's folder.
    mine = await api.client.get(url(chat, "file", str(child.id)), params={"path": "a.md"}, headers=api.auth())
    assert mine.status_code == 200, mine.text


async def test_a_service_account_of_the_org_reaches_nothing_on_the_users_computer(api, chat):
    issued = await issue_service_account_token(api.app.state.session_factory, api.org_id)
    response = await api.client.get(f"/v1/api/sessions/{chat.id}/workspace/tree", headers=api.auth(issued.token))
    assert response.status_code == 404, response.text
