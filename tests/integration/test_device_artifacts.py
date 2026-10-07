"""A chat's artifacts: a local-folder chat's are read from its folder, by its own user only; a cloud chat's are as before."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from surogates.artifacts.models import ArtifactKind
from surogates.artifacts.store import ArtifactStore
from surogates.devices.operations import OPEN_REQUESTS_PER_SESSION
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import InProcessRunner

from .test_device_files import another_member, chat  # noqa: F401  (fixtures)
from .test_devices import api, eventually, link_url  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def made_in(folder, session_id: str) -> str:
    """An artifact the agent made in *folder*, as its tool call writes it there."""
    files = DeviceWorkspaceIO(InProcessRunner(LocalWorkspaceIO(workspace_path=str(folder))), root=str(folder))
    meta = await ArtifactStore(files, session_id=UUID(session_id), root=session_id).create(
        name="notes", kind=ArtifactKind.MARKDOWN, spec={"content": "# Notes"},
    )
    return str(meta.artifact_id)


async def test_a_local_folder_chats_artifacts_are_read_from_its_folder(api, chat):
    artifact_id = await made_in(chat.folder, chat.id)
    listed = await api.client.get(f"/v1/sessions/{chat.id}/artifacts", headers=api.auth())
    assert listed.status_code == 200, listed.text
    assert [a["artifact_id"] for a in listed.json()["artifacts"]] == [artifact_id]
    opened = await api.client.get(f"/v1/sessions/{chat.id}/artifacts/{artifact_id}", headers=api.auth())
    assert opened.status_code == 200, opened.text
    assert opened.json()["spec"] == {"content": "# Notes"}
    assert "read" in chat.laptop.ran


async def test_a_page_opens_more_artifact_cards_at_once_than_a_chat_may_have_changes_waiting(api, chat):
    ids = [await made_in(chat.folder, chat.id) for _ in range(OPEN_REQUESTS_PER_SESSION + 8)]
    # As the chat page mounts every card of the thread at once: reads are bounded by their own deadline.
    answers = await asyncio.gather(*(
        api.client.get(f"/v1/sessions/{chat.id}/artifacts/{artifact_id}", headers=api.auth()) for artifact_id in ids
    ))
    assert [answer.status_code for answer in answers] == [200] * len(ids), [a.text for a in answers if a.status_code != 200][:1]


async def test_another_member_of_the_org_reaches_none_of_them(api, chat):
    artifact_id = await made_in(chat.folder, chat.id)
    token = await another_member(api)
    body = {"name": "notes", "kind": "markdown", "spec": {"content": "x"}}
    for answer in (
        await api.client.get(f"/v1/sessions/{chat.id}/artifacts", headers=api.auth(token)),
        await api.client.get(f"/v1/sessions/{chat.id}/artifacts/{artifact_id}", headers=api.auth(token)),
        # Not even that the chat is on a local folder.
        await api.client.post(f"/v1/sessions/{chat.id}/artifacts", json=body, headers=api.auth(token)),
        await api.client.put(f"/v1/sessions/{chat.id}/artifacts/{artifact_id}", json=body, headers=api.auth(token)),
    ):
        assert answer.status_code == 404, answer.text


async def test_an_artifact_is_not_made_in_a_local_folder_over_the_api(api, chat):
    artifact_id = await made_in(chat.folder, chat.id)
    body = {"name": "notes", "kind": "markdown", "spec": {"content": "x"}}
    made = await api.client.post(f"/v1/sessions/{chat.id}/artifacts", json=body, headers=api.auth())
    revised = await api.client.put(f"/v1/sessions/{chat.id}/artifacts/{artifact_id}", json=body, headers=api.auth())
    for answer in (made, revised):
        assert answer.status_code == 409, answer.text
        assert answer.json()["detail"]["error"] == "local_folder"
    opened = await api.client.get(f"/v1/sessions/{chat.id}/artifacts/{artifact_id}", headers=api.auth())
    assert (opened.json()["meta"]["version"], opened.json()["spec"]) == (1, {"content": "# Notes"})


async def test_an_offline_computer_is_said_at_once(api, chat):
    await chat.laptop.disconnect()

    async def offline() -> bool:
        response = await api.client.get(f"/v1/sessions/{chat.id}/artifacts", headers=api.auth())
        return response.status_code == 503

    await eventually(offline, timeout=10.0)


async def test_a_cloud_chats_artifacts_are_in_its_workspace_as_before(api):
    created = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    assert created.status_code == 201, created.text
    session = created.json()
    body = {"name": "notes", "kind": "markdown", "spec": {"content": "x"}}
    made = await api.client.post(f"/v1/sessions/{session['id']}/artifacts", json=body, headers=api.auth())
    assert made.status_code == 201, made.text
    artifact_id = made.json()["artifact_id"]
    key = f"{session['id']}/_artifacts/{artifact_id}/v1.json"
    assert await api.app.state.storage.read(session["config"]["storage_bucket"], key) == (
        b'{"kind": "markdown", "spec": {"content": "x"}}'
    )
    opened = await api.client.get(f"/v1/sessions/{session['id']}/artifacts/{artifact_id}", headers=api.auth())
    assert opened.json()["spec"] == {"content": "x"}
    # Its errors are as they always were: a metadata file that is not text fails the route.
    await api.app.state.storage.write(
        session["config"]["storage_bucket"], f"{session['id']}/_artifacts/{artifact_id}/meta.json", b"\xff",
    )
    with pytest.raises(UnicodeDecodeError):
        await api.client.get(f"/v1/sessions/{session['id']}/artifacts/{artifact_id}", headers=api.auth())
