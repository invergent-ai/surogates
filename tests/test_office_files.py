"""Where a document is, and the files the document editor writes it through."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.devices.binding import Binding
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.office import files as office_files
from surogates.office.agent import location_of
from surogates.office.files import EDITOR_REQUEST, FolderNotReady, document_session, editor_files, on_disk_spelling
from tests.device_computer import Computer

DEVICE = str(uuid4())
FOLDER = "/home/me/project"


def on_computer(**config):
    return SimpleNamespace(id=uuid4(), config={
        "execution": {"kind": "device", "device_id": DEVICE, "device_name": "Mac"}, "workspace_path": FOLDER,
        "storage_bucket": "b", **config,
    })


def test_a_document_on_a_computer_is_named_by_its_path_there():
    assert location_of(on_computer(), "docs/a.docx") == f"device:{DEVICE}:{FOLDER}/docs/a.docx"


def test_a_pane_path_spelled_otherwise_names_the_same_document_on_a_computer():
    assert location_of(on_computer(), "docs/./a.docx") == location_of(on_computer(), "docs//a.docx") == (
        f"device:{DEVICE}:{FOLDER}/docs/a.docx"
    )


def test_a_computer_location_is_one_unicode_form():
    nfd = "Re\u0301sume\u0301.docx"
    nfc = "R\u00e9sum\u00e9.docx"
    assert location_of(on_computer(), nfd) == location_of(on_computer(), nfc)


def _io(files, *, ignores_case):
    return DeviceWorkspaceIO(Computer(FOLDER, files, ignores_case=ignores_case), root=FOLDER, identity=f"device:{DEVICE}")


async def test_a_name_the_computer_finds_in_another_case_takes_its_listed_spelling():
    io = _io({"Docs/Plan.docx": b"x"}, ignores_case=True)
    assert await on_disk_spelling(io, "docs/plan.docx") == "Docs/Plan.docx"
    assert await on_disk_spelling(io, "Docs/Plan.docx") == "Docs/Plan.docx"


async def test_a_folder_that_tells_case_apart_keeps_the_name_as_written():
    io = _io({"plan.docx": b"x"}, ignores_case=False)
    # Not found as written: a new file, not plan.docx.
    assert await on_disk_spelling(io, "Plan.docx") == "Plan.docx"


async def test_a_name_not_there_stays_as_written_with_the_rest_of_its_path():
    io = _io({"docs/a.docx": b"x"}, ignores_case=True)
    # docs is found as written otherwise; new is not there, so it and b.docx stay as written.
    assert await on_disk_spelling(io, "Docs/new/b.docx") == "docs/new/b.docx"


def test_a_thread_and_its_master_name_a_document_alike():
    master = on_computer(workspace_boundary="workstream:p1")
    thread = on_computer(workspace_boundary="workstream:p1", execution={
        "kind": "device", "device_id": DEVICE, "history": {"thread": str(uuid4())},
    })
    assert location_of(thread, "a.docx") == location_of(master, "a.docx")


async def test_a_cloud_chat_and_a_master_are_their_own_document_sessions():
    cloud = SimpleNamespace(id=uuid4(), config={"storage_bucket": "b"})
    assert await document_session(cloud, session_factory=None) is cloud
    master = on_computer(workspace_boundary=f"workstream:{uuid4()}")
    assert await document_session(master, session_factory=None) is master


async def test_a_thread_on_a_computer_reaches_its_documents_through_its_projects_master(monkeypatch):
    project_id, master = uuid4(), on_computer()
    thread = on_computer(workspace_boundary=f"workstream:{project_id}", execution={
        "kind": "device", "device_id": DEVICE, "history": {"thread": str(uuid4())},
    })

    async def master_of(_factory, pid):
        assert str(pid) == str(project_id)
        return master

    monkeypatch.setattr(office_files, "_master_of", master_of)
    assert await document_session(thread, session_factory=object()) is master


def _recording(monkeypatch, *, state="bound"):
    given: list[dict] = []

    @asynccontextmanager
    async def fake_session_files(session, **kwargs):
        given.append(kwargs)
        yield "files"

    async def binding(_db, _root):
        return Binding(state, "not set up" if state != "bound" else None)

    @asynccontextmanager
    async def factory():
        yield None

    monkeypatch.setattr(office_files, "session_files", fake_session_files)
    monkeypatch.setattr(office_files, "binding_of", binding)
    return given, factory


async def test_an_editor_write_on_a_computer_is_a_change_request_the_desktop_knows(monkeypatch):
    given, factory = _recording(monkeypatch)
    session = on_computer()
    async with editor_files(session, storage=None, session_factory=factory, redis=None, request_id="k1.s1.0", data=b"doc") as files:
        assert files == "files"
    [kwargs] = given
    assert kwargs["request_id"] == f"{EDITOR_REQUEST}k1.s1.0"
    assert kwargs["change"] == hashlib.sha256(b"doc").hexdigest()
    assert (kwargs["access"].session_id, kwargs["access"].bound) == (session.id, True)


async def test_an_editor_write_waits_for_the_folder_to_be_set_up(monkeypatch):
    _, factory = _recording(monkeypatch, state="pending")
    with pytest.raises(FolderNotReady):
        async with editor_files(on_computer(), storage=None, session_factory=factory, redis=None, request_id="k", data=b""):
            pass


async def test_an_editor_write_in_the_cloud_is_the_chats_files(monkeypatch):
    given, factory = _recording(monkeypatch)
    cloud = SimpleNamespace(id=uuid4(), config={"storage_bucket": "b"})
    async with editor_files(cloud, storage=None, session_factory=factory, redis=None, request_id="k", data=b"doc"):
        pass
    assert given[0]["change"] is None and given[0]["access"] is None
