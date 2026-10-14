"""Opening a document in the agent's own editor session."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.devices.binding import Binding
from surogates.office import agent as office_agent
from surogates.office import files as office_files
from surogates.office import sessions as office_sessions
from surogates.office.agent import BLANK_DOCX, AgentDocumentError, location_of, open_for_agent
from surogates.office.files import DENIED_ON_COMPUTER, EDITOR_REQUEST
from surogates.office.runtime_office import page_id
from surogates.session.files import ComputerAway
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import session_workspace_key
from surogates.tools.workspace_io import RevisionConflict, StorageWorkspaceIO
from surogates.workstreams import THREAD
from tests.device_computer import computer_io

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
    runtime_office_url="http://runtime-office.test", runtime_office_token="r" * 32,
)


class FakeRuntime:
    def __init__(self) -> None:
        self.opened: list[dict] = []

    async def open(self, *, doc_key, session_id, config, document_server_url):
        self.opened.append({"doc_key": doc_key, "session_id": session_id, "config": config, "url": document_server_url})
        return page_id(doc_key, session_id)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


async def _chat(session_factory, *, config=None):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    return await store.create_session(user_id=user_id, org_id=org_id, agent_id="writer", channel="web",
                                      config={"storage_bucket": "b", **(config or {})})


async def _open(session, storage, session_factory, redis_client, runtime, path, *, create=False):
    return await open_for_agent(session=session, agent_id="writer", agent_name="Writer", path=path, create=create,
                                office=OFFICE, runtime=runtime, storage=storage, session_factory=session_factory,
                                redis=redis_client)


async def test_the_agent_joins_the_row_a_users_pane_uses(session_factory, redis_client, storage):
    session = await _chat(session_factory)
    await storage.write("b", session_workspace_key(session.id, "a.docx"), b"PK-docx")
    users = await office_sessions.open_or_reuse(session_factory, location=location_of(session, "a.docx"), path="a.docx",
                                                session_id=session.id, user=("user:1", "Flavius"))
    runtime = FakeRuntime()
    document = await _open(session, storage, session_factory, redis_client, runtime, "a.docx")
    assert document.document.id == users.id
    assert document.page_id == page_id(users.doc_key, str(session.id))
    assert document.user_id == f"agent:writer:{session.id}"
    config = runtime.opened[0]["config"]
    assert config["editorConfig"]["user"] == {"id": document.user_id, "name": "Writer"}
    assert config["editorConfig"]["mode"] == "edit"
    assert "review" not in config["editorConfig"]["customization"]
    # The connector works without plugins, and the agent's unsandboxed browser runs none.
    assert config["editorConfig"]["customization"]["plugins"] is False
    assert runtime.opened[0]["url"] == "http://docs.internal"
    assert document.document.user_names == {"user:1": "Flavius", document.user_id: "Writer"}


async def test_a_missing_document_is_made_blank_only_when_asked(session_factory, redis_client, storage):
    session = await _chat(session_factory)
    with pytest.raises(AgentDocumentError, match="No document at new/Notes.docx"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "new/Notes.docx")
    document = await _open(session, storage, session_factory, redis_client, FakeRuntime(), "new/Notes.docx", create=True)
    assert document.created is True
    assert await storage.read("b", session_workspace_key(session.id, "new/Notes.docx")) == BLANK_DOCX.read_bytes()


async def test_other_files_are_refused(session_factory, redis_client, storage):
    session = await _chat(session_factory)
    with pytest.raises(AgentDocumentError, match="Word documents"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "a.xlsx", create=True)
    with pytest.raises(AgentDocumentError, match="NUL"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "a\x00.docx", create=True)


FOLDER = "/home/me/notes"


def _computer(monkeypatch, files, device_id):
    """A fake computer answering every request of the office code; the sessions and requests asked for, kept."""
    io = computer_io(FOLDER, files, device_id=device_id)
    asked: list = []

    @asynccontextmanager
    async def fake_session_files(session, **kwargs):
        asked.append((session, kwargs))
        yield io

    async def bound(_db, _root):
        return Binding("bound")

    monkeypatch.setattr(office_agent, "session_files", fake_session_files)
    monkeypatch.setattr(office_files, "session_files", fake_session_files)
    monkeypatch.setattr(office_files, "binding_of", bound)
    return io, asked


def _on_computer(device_id, **config):
    return {"workspace_path": FOLDER, "execution": {"kind": "device", "device_id": device_id, "device_name": "Mac"}, **config}


async def test_a_document_on_the_computer_opens_in_the_agents_editor(session_factory, redis_client, storage, monkeypatch):
    device_id = str(uuid4())
    io, _ = _computer(monkeypatch, {"a.docx": b"PK"}, device_id)
    session = await _chat(session_factory, config=_on_computer(device_id))
    runtime = FakeRuntime()
    opened = await _open(session, storage, session_factory, redis_client, runtime, "a.docx", create=False)
    assert opened.document.location == f"device:{device_id}:{FOLDER}/a.docx"
    assert opened.created is False and len(runtime.opened) == 1


async def test_a_document_open_in_the_editor_is_not_looked_up_on_the_computer_again(session_factory, redis_client, storage,
                                                                                      monkeypatch):
    device_id = str(uuid4())
    _, asked = _computer(monkeypatch, {"a.docx": b"PK"}, device_id)
    session = await _chat(session_factory, config=_on_computer(device_id))
    runtime = FakeRuntime()
    await _open(session, storage, session_factory, redis_client, runtime, "a.docx")
    assert len(asked) == 1
    await _open(session, storage, session_factory, redis_client, runtime, "a.docx")
    assert len(asked) == 1 and len(runtime.opened) == 2


async def test_a_new_document_on_the_computer_is_made_through_the_editors_own_request(session_factory, redis_client, storage, monkeypatch):
    device_id = str(uuid4())
    io, asked = _computer(monkeypatch, {}, device_id)
    session = await _chat(session_factory, config=_on_computer(device_id))
    opened = await _open(session, storage, session_factory, redis_client, FakeRuntime(), "notes/Plan.docx", create=True)
    assert opened.created is True
    assert io.runner.files[f"{FOLDER}/notes/Plan.docx"][0] == BLANK_DOCX.read_bytes()
    made = [kwargs for _, kwargs in asked if kwargs.get("change")]
    assert len(made) == 1 and made[0]["request_id"].startswith(f"{EDITOR_REQUEST}new.")


async def test_a_thread_on_the_computer_opens_its_projects_real_file(session_factory, redis_client, storage, monkeypatch):
    device_id, project_id = str(uuid4()), uuid4()
    io, asked = _computer(monkeypatch, {"a.docx": b"real"}, device_id)
    master = await _chat(session_factory, config=_on_computer(device_id, workspace_boundary=f"workstream:{project_id}"))
    thread = await _chat(session_factory, config=_on_computer(
        device_id, workspace_boundary=f"workstream:{project_id}", workstream_role=THREAD,
        execution={"kind": "device", "device_id": device_id, "history": {"thread": str(uuid4())}},
    ))

    async def master_of(_factory, pid):
        assert pid == project_id
        return master

    monkeypatch.setattr(office_files, "_master_of", master_of)
    opened = await _open(thread, storage, session_factory, redis_client, FakeRuntime(), "a.docx", create=False)
    assert {session.id for session, _ in asked} == {master.id}
    assert opened.document.location == f"device:{device_id}:{FOLDER}/a.docx"


async def test_open_locations_names_the_open_ones(session_factory, storage):
    session = await _chat(session_factory)
    location = location_of(session, "a.docx")
    await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=session.id)
    assert await office_sessions.open_locations(session_factory, [location, location_of(session, "b.docx")]) == {location}
    assert (await office_sessions.open_at(session_factory, location)).path == "a.docx"
    assert await office_sessions.open_at(session_factory, location_of(session, "b.docx")) is None


async def test_a_thread_is_told_its_documents_reach_the_project_when_the_turn_ends(session_factory, redis_client, storage):
    thread = await _chat(session_factory, config={"workstream_role": THREAD, "workspace_boundary": f"workstream:{uuid4()}"})
    with pytest.raises(AgentDocumentError, match="in the project's files"):
        await _open(thread, storage, session_factory, redis_client, FakeRuntime(), "memo.docx")


async def test_a_document_made_meanwhile_is_opened_not_replaced(session_factory, redis_client, storage, monkeypatch):
    session = await _chat(session_factory)
    key = session_workspace_key(session.id, "a.docx")

    async def made_meanwhile(self, file_key, data, *, expected):
        await storage.write("b", key, b"PK-theirs")
        raise RevisionConflict(file_key)

    monkeypatch.setattr(StorageWorkspaceIO, "write_if", made_meanwhile)
    document = await _open(session, storage, session_factory, redis_client, FakeRuntime(), "a.docx", create=True)
    assert document.created is False
    assert await storage.read("b", key) == b"PK-theirs"


async def test_a_document_still_closing_is_refused_after_the_wait(session_factory, redis_client, storage, monkeypatch):
    monkeypatch.setattr(office_sessions, "CLOSING_WAIT_S", 0.2)
    monkeypatch.setattr(office_sessions, "CLOSING_POLL_S", 0.05)
    session = await _chat(session_factory)
    await storage.write("b", session_workspace_key(session.id, "a.docx"), b"PK-docx")
    users = await office_sessions.open_or_reuse(session_factory, location=location_of(session, "a.docx"), path="a.docx",
                                                session_id=session.id)
    await office_sessions.set_pending(session_factory, users.id, bucket="b", key="k", close=True)
    with pytest.raises(AgentDocumentError, match="still being saved"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "a.docx")


def _answers(io, kind, error):
    """The fake computer answers every *kind* operation with *error*."""
    run = io.runner.run

    async def answer(asked, args, payload=None):
        return {"error": error} if asked == kind else await run(asked, args, payload)

    io.runner.run = answer


async def test_a_folder_not_set_up_on_the_computer_is_said(session_factory, redis_client, storage, monkeypatch):
    device_id = str(uuid4())
    _computer(monkeypatch, {}, device_id)

    async def pending(_db, _root):
        return Binding("pending", "not set up")

    monkeypatch.setattr(office_files, "binding_of", pending)
    session = await _chat(session_factory, config=_on_computer(device_id))
    with pytest.raises(AgentDocumentError, match="not set up"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "a.docx", create=True)


async def test_a_computer_away_is_named(session_factory, redis_client, storage, monkeypatch):
    device_id = str(uuid4())

    @asynccontextmanager
    async def away(session, **kwargs):
        raise ComputerAway("Mac", revoked=False)
        yield

    monkeypatch.setattr(office_agent, "session_files", away)
    session = await _chat(session_factory, config=_on_computer(device_id))
    with pytest.raises(AgentDocumentError, match="Mac"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "a.docx")


async def test_what_the_computer_answered_is_said(session_factory, redis_client, storage, monkeypatch):
    device_id = str(uuid4())
    io, _ = _computer(monkeypatch, {"a.docx": b"PK"}, device_id)
    _answers(io, "stat", {"type": "unavailable", "message": "The folder's drive is not connected"})
    session = await _chat(session_factory, config=_on_computer(device_id))
    with pytest.raises(AgentDocumentError, match="The folder's drive is not connected"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "a.docx")


@pytest.mark.parametrize(("message", "said"), [
    (DENIED_ON_COMPUTER, "notes/Plan.docx was not made: the computer's user declined it."),
    ("Operation not permitted: the folder is read-only", "Operation not permitted: the folder is read-only"),
])
async def test_only_the_users_no_reads_as_declined(session_factory, redis_client, storage, monkeypatch, message, said):
    device_id = str(uuid4())
    io, _ = _computer(monkeypatch, {}, device_id)
    _answers(io, "write", {"type": "os", "code": "EACCES", "message": message})
    session = await _chat(session_factory, config=_on_computer(device_id))
    with pytest.raises(AgentDocumentError) as refused:
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "notes/Plan.docx", create=True)
    assert str(refused.value) == said
