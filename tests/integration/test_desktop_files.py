"""A local-folder chat's file panel over the real desktop app: the folder its user confirmed, asking every time."""

from __future__ import annotations

import pytest

from .test_desktop_file_operations import journal_dir, prepare  # noqa: F401  (fixture)
from .test_desktop_link_client import built_client, client, connected  # noqa: F401  (fixture)
from .test_device_sessions import is_bound, local_chat
from .test_devices import api, eventually, link_url, register  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]


async def test_the_file_panel_shows_and_changes_the_folder_on_the_real_app(built_client, api, link_url, tmp_path, journal_dir):
    folder = prepare(tmp_path)
    device = await register(api)
    # Asking every time; its user denies what names "secret".
    app = await client(built_client, link_url, device["token"], journal_dir / "journal.sqlite", confirm=folder, ask="secret")
    try:
        await app.until(lambda events: any(e["event"] == "prepared" for e in events), timeout=30.0)
        prepared = next(e for e in app.events if e["event"] == "prepared")
        await app.until(connected, timeout=30.0)
        session_id = await local_chat(api, device["id"], folder=prepared["folder"], nonce=prepared["nonce"])
        await eventually(lambda: is_bound(api, session_id), timeout=10.0)
        files = f"/v1/sessions/{session_id}/workspace"

        tree = await api.client.get(f"{files}/tree", headers=api.auth())
        assert tree.status_code == 200, tree.text
        names = {entry["name"] for entry in tree.json()["entries"]}
        assert {"a.txt", "sub", "pages"} <= names and ".git" not in names
        opened = await api.client.get(f"{files}/file", params={"path": "a.txt"}, headers=api.auth())
        assert (opened.status_code, opened.json()["content"]) == (200, "alpha\nbeta\n")
        downloaded = await api.client.get(f"{files}/download", params={"path": "sub/b.txt"}, headers=api.auth())
        assert (downloaded.status_code, downloaded.content) == (200, b"beta gamma\n")

        allowed = await api.client.post(
            f"{files}/upload", params={"path": "uploads", "request_id": "upload-000000000010"},
            files={"file": ("notes.txt", b"draft")}, headers=api.auth(),
        )
        assert allowed.status_code == 201, allowed.text
        assert (folder / "uploads" / "notes.txt").read_bytes() == b"draft"
        denied = await api.client.post(
            f"{files}/upload", params={"request_id": "upload-000000000011"},
            files={"file": ("secret.txt", b"no")}, headers=api.auth(),
        )
        assert (denied.status_code, denied.json()["detail"]) == (403, "The user denied this change on this computer")
        assert not (folder / "secret.txt").exists()
        asked = sum(1 for e in app.events if e["event"] == "approval")

        # The page's whiteboard saves are never asked about.
        canvas = await api.client.post(
            f"{files}/upload", params={"path": "_whiteboard", "request_id": "upload-000000000012"},
            files={"file": ("canvas.json", b"{}")}, headers=api.auth(),
        )
        assert canvas.status_code == 201, canvas.text
        assert sum(1 for e in app.events if e["event"] == "approval") == asked
    finally:
        await app.close()
