"""A local-folder chat's file panel: what it shows and opens is in the folder on the user's computer."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio

import surogates.api.routes.workspace as workspace_routes
import surogates.devices.operations as operations_module
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


async def upload(api, chat, name: str, data: bytes, *, token: str | None = None, **params):
    return await api.client.post(
        url(chat, "upload"), params=params, files={"file": (name, data)}, headers=api.auth(token),
    )


async def test_an_upload_lands_in_the_folder(api, chat):
    response = await upload(api, chat, "notes.txt", b"draft", path="uploads", request_id="upload-000000000001")
    assert response.status_code == 201, response.text
    assert response.json() == {"path": "uploads/notes.txt", "size": 5}
    assert (chat.folder / "uploads" / "notes.txt").read_bytes() == b"draft"


async def test_a_delete_removes_the_file_from_the_folder(api, chat):
    (chat.folder / "old.txt").write_text("old")
    response = await api.client.delete(url(chat, "file"), params={"path": "old.txt"}, headers=api.auth())
    assert response.status_code == 200, response.text
    assert not (chat.folder / "old.txt").exists()
    again = await api.client.delete(url(chat, "file"), params={"path": "old.txt"}, headers=api.auth())
    assert again.status_code == 404, again.text


async def test_another_member_of_the_org_changes_nothing_on_the_users_computer(api, chat):
    stranger = await another_member(api)
    (chat.folder / "old.txt").write_text("old")
    uploaded = await upload(api, chat, "notes.txt", b"theirs", token=stranger, request_id="upload-000000000020")
    deleted = await api.client.delete(url(chat, "file"), params={"path": "old.txt"}, headers=api.auth(stranger))
    assert (uploaded.status_code, deleted.status_code) == (404, 404)
    assert not (chat.folder / "notes.txt").exists()
    assert (chat.folder / "old.txt").read_text() == "old"


async def test_a_change_its_user_has_not_allowed_is_kept_and_the_same_request_gets_what_happened(
    api, chat, monkeypatch,
):
    monkeypatch.setattr(workspace_routes, "CHANGE_WITHIN_S", 0.5)
    chat.laptop.hold_asked = True  # its write is recorded, and waits for its user's answer
    waiting = await upload(api, chat, "notes.txt", b"draft", request_id="upload-000000000002")
    assert waiting.status_code == 202, waiting.text
    assert waiting.json()["request_id"] == "upload-000000000002"
    assert not chat.laptop.cancelled
    # Allowed now: the computer answers what it held when it is next asked.
    chat.laptop.hold_asked = False
    await chat.laptop.disconnect()
    await chat.laptop.connect()
    for _ in range(2):  # and the same request once more, which changes nothing again
        done = await upload(api, chat, "notes.txt", b"draft", request_id="upload-000000000002")
        assert done.status_code == 201, done.text
    assert (chat.folder / "notes.txt").read_bytes() == b"draft"
    assert chat.laptop.ran.count("write") == 1


async def test_an_upload_over_one_frame_lands_whole(api, chat):
    data = bytes(range(256)) * 8192
    response = await upload(api, chat, "big.bin", data, request_id="upload-000000000004")
    assert response.status_code == 201, response.text
    assert (chat.folder / "big.bin").read_bytes() == data


async def test_a_request_id_sent_again_with_another_change_is_refused_and_changes_nothing(api, chat, monkeypatch):
    monkeypatch.setattr(workspace_routes, "CHANGE_WITHIN_S", 0.5)
    chat.laptop.hold_asked = True
    first = await upload(api, chat, "notes.txt", b"one", request_id="upload-000000000005")
    assert first.status_code == 202, first.text  # its write is recorded, and waits
    again = await upload(api, chat, "notes.txt", b"two", request_id="upload-000000000005")
    assert again.status_code == 409, again.text
    chat.laptop.hold_asked = False
    await chat.laptop.disconnect()
    await chat.laptop.connect()
    landed = await upload(api, chat, "notes.txt", b"one", request_id="upload-000000000005")
    assert landed.status_code == 201, landed.text
    assert (chat.folder / "notes.txt").read_bytes() == b"one"


async def test_a_changed_request_is_refused_before_its_computer_has_seen_any_of_it(api, chat, monkeypatch):
    monkeypatch.setattr(workspace_routes, "CHANGE_WITHIN_S", 0.5)
    chat.laptop.hold = True  # not even its first step is answered
    first = await upload(api, chat, "notes.txt", b"one", request_id="upload-000000000009")
    assert first.status_code == 202, first.text
    again = await upload(api, chat, "notes.txt", b"two", request_id="upload-000000000009")
    assert again.status_code == 409, again.text


async def test_a_chat_may_have_only_so_many_changes_waiting(api, chat, monkeypatch):
    monkeypatch.setattr(workspace_routes, "CHANGE_WITHIN_S", 0.5)
    monkeypatch.setattr(operations_module, "OPEN_REQUESTS_PER_SESSION", 1)
    chat.laptop.hold_asked = True
    waiting = await upload(api, chat, "one.txt", b"one", request_id="upload-000000000030")
    assert waiting.status_code == 202, waiting.text
    refused = await upload(api, chat, "two.txt", b"two", request_id="upload-000000000031")
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"]["error"] == "device_busy"


async def test_a_path_through_a_file_or_naming_a_folder_is_answered_in_the_computers_words(api, chat):
    (chat.folder / "a.txt").write_text("a")
    (chat.folder / "sub").mkdir()
    under = await upload(api, chat, "notes.txt", b"x", path="a.txt", request_id="upload-000000000006")
    assert under.status_code == 409, under.text
    folder = await api.client.delete(url(chat, "file"), params={"path": "sub"}, headers=api.auth())
    assert folder.status_code == 404, folder.text
    assert (chat.folder / "sub").is_dir()


async def test_a_name_too_long_for_the_folder_is_the_users_error(api, chat):
    response = await upload(api, chat, f"{'n' * 300}.txt", b"x", request_id="upload-000000000010")
    assert response.status_code == 400, response.text


async def test_two_changes_to_one_file_at_once_are_both_answered(api, chat):
    answers = await asyncio.gather(
        upload(api, chat, "notes.txt", b"one", request_id="upload-000000000007"),
        upload(api, chat, "notes.txt", b"two", request_id="upload-000000000008"),
    )
    assert [response.status_code for response in answers] == [201, 201]
    assert (chat.folder / "notes.txt").read_bytes() in (b"one", b"two")


async def test_a_change_to_an_offline_computers_folder_is_refused_at_once(api, chat):
    await chat.laptop.disconnect()

    async def offline() -> bool:
        return (await upload(api, chat, "notes.txt", b"draft", request_id="upload-000000000003")).status_code == 503

    await eventually(offline, timeout=10.0)
    assert not (chat.folder / "notes.txt").exists()


async def test_a_request_id_is_a_short_token(api, chat):
    response = await upload(api, chat, "notes.txt", b"draft", request_id="../../x")
    assert response.status_code == 422, response.text


async def test_a_messages_attachment_is_read_from_the_folder(api, chat):
    (chat.folder / "uploads").mkdir()
    (chat.folder / "uploads" / "notes.txt").write_text("draft from the laptop")
    response = await api.client.post(
        f"/v1/sessions/{chat.id}/messages",
        json={"content": "Tidy these", "attachments": [
            {"path": "uploads/notes.txt", "filename": "notes.txt", "mime_type": "text/plain", "size": 1},
        ]},
        headers=api.auth(),
    )
    assert response.status_code == 202, response.text
    [event] = [e for e in await api.app.state.session_store.get_events(UUID(chat.id)) if e.type == "user.message"]
    [attachment] = event.data["attachments"]
    assert (attachment["size"], attachment["inlined_text"]) == (21, "draft from the laptop")


async def test_an_attachment_missing_from_the_folder_is_refused(api, chat):
    response = await api.client.post(
        f"/v1/sessions/{chat.id}/messages",
        json={"content": "Tidy these", "attachments": [{"path": "uploads/gone.txt", "filename": "gone.txt"}]},
        headers=api.auth(),
    )
    assert response.status_code == 422, response.text
    assert "uploads/gone.txt" in response.json()["detail"]


async def test_an_attachment_path_the_journal_cannot_take_is_the_users_error(api, chat):
    response = await api.client.post(
        f"/v1/sessions/{chat.id}/messages",
        content=b'{"content": "Tidy these", "attachments": [{"path": "\\ud800.txt", "filename": "x.txt"}]}',
        headers={**api.auth(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400, response.text


async def test_another_member_of_the_org_attaches_nothing_from_the_users_computer(api, chat):
    stranger = await another_member(api)
    (chat.folder / "notes.txt").write_text("private")
    ran = list(chat.laptop.ran)
    response = await api.client.post(
        f"/v1/sessions/{chat.id}/messages",
        json={"content": "Show me", "attachments": [{"path": "notes.txt", "filename": "notes.txt"}]},
        headers=api.auth(stranger),
    )
    assert response.status_code == 404, response.text
    assert chat.laptop.ran == ran
