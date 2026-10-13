"""Opening a document in the agent's own editor session."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import sessions as office_sessions
from surogates.office.agent import BLANK_DOCX, AgentDocumentError, location_of, open_for_agent
from surogates.office.runtime_office import page_id
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import session_workspace_key

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


async def test_other_files_and_computer_folders_are_refused(session_factory, redis_client, storage):
    session = await _chat(session_factory)
    with pytest.raises(AgentDocumentError, match=r"\.docx"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "sheet.xlsx")
    on_a_computer = await _chat(session_factory, config={"execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"}})
    with pytest.raises(AgentDocumentError, match="computer"):
        await _open(on_a_computer, storage, session_factory, redis_client, FakeRuntime(), "a.docx", create=True)


async def test_open_locations_names_the_open_ones(session_factory, storage):
    session = await _chat(session_factory)
    location = location_of(session, "a.docx")
    await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=session.id)
    assert await office_sessions.open_locations(session_factory, [location, location_of(session, "b.docx")]) == {location}
    assert (await office_sessions.open_at(session_factory, location)).path == "a.docx"
    assert await office_sessions.open_at(session_factory, location_of(session, "b.docx")) is None
