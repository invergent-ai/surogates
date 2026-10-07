"""A local folder's artifacts: kept among the harness's own files there, one folder per root chat, made through the call that makes them."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import surogates.artifacts.store as store_module
from surogates.artifacts.models import MAX_ARTIFACT_BYTES, ArtifactKind
from surogates.artifacts.store import ArtifactLimitError, ArtifactNotFoundError, ArtifactStore, FolderArtifacts
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.session.events import EventType
from surogates.tools.builtin.artifact import _create_artifact_handler
from surogates.tools.builtin.artifact import register as register_artifact_tool
from surogates.tools.registry import ToolRegistry
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


async def test_create_artifact_on_a_local_folder_makes_it_there_without_the_api(files, folder):
    events, session_id, root = Events(), uuid4(), str(uuid4())
    made = json.loads(await _create_artifact_handler(
        {"name": "notes", "kind": "markdown", "spec": {"content": "# Notes"}},
        workspace_io=files, session_store=events, session_id=str(session_id), task_id=root,
    ))
    assert made["success"] is True, made
    # Under its root chat's folder: a sub-agent's call makes them where its root lists them.
    assert (folder / ".surogates-results" / "artifacts" / root / made["artifact_id"] / "v1.json").is_file()
    assert [kind for _, kind, _ in events.emitted] == [EventType.ARTIFACT_CREATED]


async def test_a_failed_revision_on_a_local_folder_hands_back_what_the_folder_holds(files, monkeypatch):
    kwargs = {"workspace_io": files, "session_store": Events(), "session_id": str(uuid4()), "task_id": str(uuid4())}
    made = json.loads(await _create_artifact_handler({"name": "t", "kind": "markdown", "spec": {"content": "a"}}, **kwargs))
    monkeypatch.setattr(store_module, "MAX_ARTIFACT_BYTES", 10)
    failed = json.loads(await _create_artifact_handler(
        {"name": "t", "kind": "markdown", "spec": {"content": "a much longer body"}, "artifact_id": made["artifact_id"]},
        **kwargs,
    ))
    assert failed["success"] is False
    assert failed["current"] == {"kind": "markdown", "spec": {"content": "a"}, "version": 1}


async def test_a_folders_artifacts_are_kept_under_a_root_that_names_a_chat(files):
    with pytest.raises(ValueError):
        ArtifactStore(files, session_id=uuid4(), root="../../src")


async def test_an_index_entry_that_is_not_an_artifact_is_passed_over_with_a_warning(files, folder, caplog):
    root = str(uuid4())
    store = ArtifactStore(files, session_id=uuid4(), root=root)
    made = await store.create(name="notes", kind=ArtifactKind.MARKDOWN, spec={"content": "x"})
    index = folder / ".surogates-results" / "artifacts" / root / "index.json"
    # Anything on the computer may write it: the chat's artifacts go on.
    index.write_text(json.dumps(["planted", {"artifact_id": "not one"}, *json.loads(index.read_text())]))
    with caplog.at_level(logging.WARNING, logger="surogates.artifacts.store"):
        assert [m.artifact_id for m in await store.list()] == [made.artifact_id]
    assert "index" in caplog.text
    await store.update(made.artifact_id, name="notes", kind=ArtifactKind.MARKDOWN, spec={"content": "y"})
    assert [(m.artifact_id, m.version) for m in await store.list()] == [(made.artifact_id, 2)]


class Reads(InProcessRunner):
    """The computer, recording how much of each file a read asked for."""

    def __init__(self, folder) -> None:
        super().__init__(LocalWorkspaceIO(workspace_path=str(folder)))
        self.asked: list = []

    async def run(self, kind, args, payload=None):
        if kind == "read":
            self.asked.append(args["max_bytes"])
        return await super().run(kind, args, payload)


async def test_a_folders_artifact_files_are_read_no_further_than_an_artifact_and_a_longer_one_is_corrupted(folder):
    runner, root = Reads(folder), str(uuid4())
    store = ArtifactStore(DeviceWorkspaceIO(runner, root=str(folder)), session_id=uuid4(), root=root)
    made = await store.create(name="notes", kind=ArtifactKind.MARKDOWN, spec={"content": "x"})
    base = folder / ".surogates-results" / "artifacts" / root
    # Still JSON, read whole, but longer than anything the store writes.
    meta = base / str(made.artifact_id) / "meta.json"
    meta.write_text(meta.read_text() + " " * MAX_ARTIFACT_BYTES)
    with pytest.raises(ValueError):
        await store.get_meta(made.artifact_id)
    index = base / "index.json"
    index.write_text(index.read_text() + " " * MAX_ARTIFACT_BYTES)
    assert await store.list() == []
    assert set(runner.asked) == {MAX_ARTIFACT_BYTES + 1}


async def test_what_the_model_sees_when_its_computer_refuses_an_artifact_or_is_away(folder):
    registry = ToolRegistry()
    register_artifact_tool(registry)
    args = {"name": "notes", "kind": "markdown", "spec": {"content": "# Notes"}}

    class Revoked(InProcessRunner):
        async def run(self, kind, args, payload=None):
            if kind == "write":
                return {"error": {"type": "revoked", "message": "Local access to this computer was revoked"}}
            return await super().run(kind, args, payload)

    def call(runner) -> dict:
        return {
            "workspace_io": DeviceWorkspaceIO(runner, root=str(folder)), "session_store": Events(),
            "session_id": str(uuid4()), "task_id": str(uuid4()),
        }

    refused = await registry.dispatch("create_artifact", args, **call(Revoked(LocalWorkspaceIO(workspace_path=str(folder)))))
    assert json.loads(refused) == {"error": "Tool execution failed: Local access to this computer was revoked"}

    class Away(InProcessRunner):
        """Offline: inside a tool call, its operations wait for it."""

        def __init__(self, folder) -> None:
            super().__init__(LocalWorkspaceIO(workspace_path=str(folder)))
            self.back = asyncio.Event()

        async def run(self, kind, args, payload=None):
            await self.back.wait()
            return await super().run(kind, args, payload)

    away = Away(folder)
    waiting = asyncio.ensure_future(registry.dispatch("create_artifact", args, **call(away)))
    await asyncio.sleep(0.2)
    # No error, and no deadline of its own: the call waits for its computer.
    assert not waiting.done()
    away.back.set()
    assert json.loads(await asyncio.wait_for(waiting, 5.0))["success"] is True
