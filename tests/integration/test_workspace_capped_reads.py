"""The workspace panel's file routes fetch no more of an object than they can use."""

from __future__ import annotations

import base64
from uuid import UUID

import pytest

from surogates.api.routes import workspace as workspace_route
from surogates.storage.backend import LocalBackend

from .test_devices import api  # noqa: F401  (api is a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


class Counting(LocalBackend):
    """Local storage that says how many whole-object reads it was asked for."""

    whole = 0

    async def read(self, bucket, key):
        type(self).whole += 1
        return await super().read(bucket, key)


async def a_file(api, name: str, data: bytes):  # noqa: F811
    Counting.whole = 0
    api.app.state.storage = Counting(api.app.state.storage._base)
    created = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    assert created.status_code == 201, created.text
    session = await api.app.state.session_store.get_session(UUID(created.json()["id"]))
    await api.app.state.storage.write(session.config["storage_bucket"], f"{session.id}/{name}", data)
    return session


async def test_a_large_text_preview_fetches_only_what_it_shows_and_says_the_real_size(api):  # noqa: F811
    data = b"x" * (workspace_route._MAX_READ_BYTES + 5000)
    session = await a_file(api, "big.txt", data)
    answer = await api.client.get(
        f"/v1/sessions/{session.id}/workspace/file", params={"path": "big.txt"}, headers=api.auth(),
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["truncated"] is True and body["size"] == len(data)
    assert len(body["content"]) == workspace_route._MAX_READ_BYTES
    assert Counting.whole == 0


async def test_a_text_file_of_exactly_the_limit_is_whole(api):  # noqa: F811
    data = b"x" * workspace_route._MAX_READ_BYTES
    session = await a_file(api, "exact.txt", data)
    answer = await api.client.get(
        f"/v1/sessions/{session.id}/workspace/file", params={"path": "exact.txt"}, headers=api.auth(),
    )
    body = answer.json()
    assert body["truncated"] is False and body["size"] == len(data) and len(body["content"]) == len(data)


async def test_an_image_over_its_limit_is_refused_with_its_size_unfetched(api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(workspace_route, "_MAX_IMAGE_BYTES", 10)
    data = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    session = await a_file(api, "big.png", data)
    answer = await api.client.get(
        f"/v1/sessions/{session.id}/workspace/file", params={"path": "big.png"}, headers=api.auth(),
    )
    assert answer.status_code == 413 and str(len(data)) in answer.text
    assert Counting.whole == 0


async def test_an_image_within_its_limit_is_sent_whole(api):  # noqa: F811
    data = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    session = await a_file(api, "small.png", data)
    answer = await api.client.get(
        f"/v1/sessions/{session.id}/workspace/file", params={"path": "small.png"}, headers=api.auth(),
    )
    assert base64.b64decode(answer.json()["content"]) == data


async def test_a_download_is_read_no_further_than_its_cap(api, monkeypatch):  # noqa: F811
    data = b"0123456789"
    session = await a_file(api, "a.bin", data)
    answer = await api.client.get(
        f"/v1/sessions/{session.id}/workspace/download", params={"path": "a.bin"}, headers=api.auth(),
    )
    assert answer.status_code == 200 and answer.content == data and Counting.whole == 0
