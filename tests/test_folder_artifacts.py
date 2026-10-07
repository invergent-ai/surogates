"""A local folder's artifacts: kept among the harness's own files there, one folder per root chat, made through the call that makes them."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import surogates.artifacts.store as store_module
from surogates.artifacts.models import ArtifactKind
from surogates.artifacts.store import ArtifactLimitError, ArtifactNotFoundError, ArtifactStore, FolderArtifacts
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.session.events import EventType
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import InProcessRunner

pytestmark = pytest.mark.asyncio


class Events:
    """The session store's emit_event, recorded."""

    def __init__(self) -> None:
        self.emitted: list[tuple] = []

    async def emit_event(self, session_id, kind, data) -> int:
        self.emitted.append((session_id, kind, data))
        return len(self.emitted)


@pytest.fixture
def folder(tmp_path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def files(folder) -> DeviceWorkspaceIO:
    return DeviceWorkspaceIO(InProcessRunner(LocalWorkspaceIO(workspace_path=str(folder))), root=str(folder))


async def test_a_folders_artifacts_are_among_the_harness_files_under_their_root_chat(files, folder):
    root = str(uuid4())
    store = ArtifactStore(files, session_id=uuid4(), root=root)
    meta = await store.create(name="notes", kind=ArtifactKind.MARKDOWN, spec={"content": "# Notes"})
    base = folder / ".surogates-results" / "artifacts" / root
    assert json.loads((base / str(meta.artifact_id) / "v1.json").read_text()) == {
        "kind": "markdown", "spec": {"content": "# Notes"},
    }
    assert [entry["artifact_id"] for entry in json.loads((base / "index.json").read_text())] == [str(meta.artifact_id)]
    # Not the cloud's _artifacts/: Ask every time would ask about each of its files.
    assert not (folder / "_artifacts").exists()
    assert [m.artifact_id for m in await store.list()] == [meta.artifact_id]
    assert (await store.get_payload(meta.artifact_id))["spec"] == {"content": "# Notes"}
    with pytest.raises(ArtifactNotFoundError):
        await store.get_meta(uuid4())


async def test_two_chats_on_one_folder_keep_their_own_artifacts_and_their_own_cap(files, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_ARTIFACTS_PER_SESSION", 1)
    first, later = str(uuid4()), str(uuid4())
    made = await ArtifactStore(files, session_id=UUID(first), root=first).create(
        name="a", kind=ArtifactKind.MARKDOWN, spec={"content": "a"},
    )
    # A sub-agent of the first chat shares its root's, as in the cloud.
    assert [m.artifact_id for m in await ArtifactStore(files, session_id=uuid4(), root=first).list()] == [made.artifact_id]
    # A later chat on the same folder sees none of them, and has its own cap.
    store = ArtifactStore(files, session_id=UUID(later), root=later)
    assert await store.list() == []
    await store.create(name="b", kind=ArtifactKind.MARKDOWN, spec={"content": "b"})
    with pytest.raises(ArtifactLimitError):
        await ArtifactStore(files, session_id=UUID(first), root=first).create(
            name="c", kind=ArtifactKind.MARKDOWN, spec={"content": "c"},
        )


async def test_a_local_folders_artifacts_need_their_root_chat(files):
    with pytest.raises(ValueError, match="root chat"):
        ArtifactStore(files, session_id=uuid4())


async def test_an_index_that_is_not_text_is_reset_and_the_chats_artifacts_go_on(files, folder):
    root = str(uuid4())
    index = folder / ".surogates-results" / "artifacts" / root / "index.json"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"\x89PNG\r\n\x1a\n")
    store = ArtifactStore(files, session_id=uuid4(), root=root)
    assert await store.list() == []
    meta = await store.create(name="notes", kind=ArtifactKind.MARKDOWN, spec={"content": "x"})
    assert [m.artifact_id for m in await store.list()] == [meta.artifact_id]


async def test_a_tools_artifact_is_made_in_the_folder_and_announced(files):
    events, session_id = Events(), uuid4()
    artifacts = FolderArtifacts(files, events, session_id, str(session_id))
    made = json.loads(await artifacts.create_artifact(name="chart", kind="markdown", spec={"content": "a"}))
    assert made["success"] is True and made["version"] == 1
    revised = json.loads(await artifacts.create_artifact(
        name="chart", kind="markdown", spec={"content": "b"}, artifact_id=made["artifact_id"],
    ))
    assert revised["version"] == 2
    assert [(sid, kind) for sid, kind, _ in events.emitted] == [
        (session_id, EventType.ARTIFACT_CREATED), (session_id, EventType.ARTIFACT_UPDATED),
    ]
    assert events.emitted[1][2] == {
        "artifact_id": made["artifact_id"], "name": "chart", "kind": "markdown", "version": 2, "size": revised["size"],
    }
    stored = await artifacts.get_artifact(made["artifact_id"])
    assert (stored["kind"], stored["spec"], stored["meta"]["version"]) == ("markdown", {"content": "b"}, 2)


async def test_what_the_routes_would_refuse_is_refused_and_announces_nothing(files):
    events, session_id = Events(), uuid4()
    artifacts = FolderArtifacts(files, events, session_id, str(session_id))
    for refused in (
        artifacts.create_artifact(name="", kind="markdown", spec={"content": "a"}),
        artifacts.create_artifact(name="t", kind="table", spec={"columns": "x"}),
        artifacts.create_artifact(name="t", kind="markdown", spec={"content": "a"}, artifact_id=str(uuid4())),
        artifacts.create_artifact(name="t", kind="markdown", spec={"content": "a"}, artifact_id="not-an-id"),
    ):
        assert json.loads(await refused)["success"] is False
    assert events.emitted == []
    assert await artifacts.get_artifact(str(uuid4())) is None
    assert await artifacts.get_artifact("not-an-id") is None
