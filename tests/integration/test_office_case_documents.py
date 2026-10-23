"""A case's documents in the editor: opened, saved and kept open with no chat behind them.

A case's document is the case's, not a chat's: its row is opened for the
case, the document server reads it from and saves it into the case's
workspace, and no chat going away closes it.  A row nobody ever opened, or
everyone has left, still closes once it has been idle.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from surogates.api.routes import office as office_route
from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.keel.case_files import case_workspace, write_case_file
from surogates.office import reaper
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of
from surogates.office.saves import land_pending_saves
from surogates.office.tokens import sign, url_token
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend

from .test_keel_step_sessions import (  # noqa: F401  (link, link_app and org are fixtures)
    body,
    contract_of,
    link,
    link_app,
    org,
    post,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

JWT_SECRET = "j" * 32
TOKEN_SECRET = "t" * 32
OFFICE = OfficeSettings(
    public_url="https://office.example",
    internal_url="http://docs.internal",
    callback_base_url="https://api.example",
    jwt_secret=JWT_SECRET,
    token_secret=TOKEN_SECRET,
)
# Past the span the reaper leaves an idle row open.
IDLE = reaper.NEVER_OPENED_AFTER + timedelta(minutes=1)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    """Each test sees only its own rows: the reaper and the save job scan the whole table."""
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()
    application.state.settings.storage.bucket = f"test-agent-{uuid.uuid4()}"
    application.state.settings.office = OFFICE
    application.state.storage = create_backend(application.state.settings)
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


async def _reap(session_factory, storage, *, now=None):
    return await reaper.reap(session_factory=session_factory, session_store=SessionStore(session_factory), office=OFFICE,
                             storage=storage, now=now)


async def _open_for_case(session_factory, case_id):
    return await office_sessions.open_waiting(
        session_factory, location=f"bucket:b/cases/{case_id}/pack.docx", path="pack.docx",
        session_id=None, case_id=case_id,
    )


async def test_a_case_document_without_a_chat_saves_and_is_not_reaped(client, app, session_factory, redis_client, monkeypatch):
    saves: list[str] = []

    async def save_served_by_the_document_server(url: str) -> bytes:
        saves.append(url)
        return b"PK-edited"

    monkeypatch.setattr(office_route, "_download", save_served_by_the_document_server)
    case_id = uuid.uuid4()
    storage, settings = app.state.storage, app.state.settings
    await write_case_file(storage, settings, case_id, "pack.docx", b"PK-as-sent")
    workspace = case_workspace(storage, settings, case_id)
    document = await office_sessions.open_waiting(
        session_factory, location=workspace.location("pack.docx"), path="pack.docx", session_id=None, case_id=case_id,
    )

    fetched = await client.get(f"/v1/office/files/{url_token(TOKEN_SECRET, document.id, 'file')}")

    assert fetched.status_code == 200, fetched.text
    assert fetched.content == b"PK-as-sent"
    callback = f"/v1/office/callback/{url_token(TOKEN_SECRET, document.id, 'callback')}"
    joined = {"key": document.doc_key, "status": 1, "users": ["user:1"]}
    assert (await client.post(callback, json={"token": sign(JWT_SECRET, joined)})).json() == {"error": 0}
    assert await _reap(session_factory, storage) == 0
    assert await office_sessions.get_open(session_factory, document.id) is not None

    saved = {"key": document.doc_key, "status": 2, "url": "https://office.example/cache/files/pack/output.docx"}
    answer = await client.post(callback, json={"token": sign(JWT_SECRET, saved)})

    assert answer.json() == {"error": 0}, answer.text
    assert saves == ["http://docs.internal/cache/files/pack/output.docx"]
    assert (await office_sessions.get_open(session_factory, document.id)).pending_save is not None
    landed = await land_pending_saves(session_factory=session_factory, session_store=app.state.session_store,
                                      storage=storage, redis=redis_client, office=OFFICE, settings=settings)
    assert landed == 1
    assert await workspace.read("pack.docx") == b"PK-edited"
    # The document server's save on close ends the row.
    assert await office_sessions.get_open(session_factory, document.id) is None


async def test_a_step_session_and_a_person_share_one_case_document(link, link_app, org, session_store, session_factory):
    created = await post(link, body(org))
    assert created.status_code == 201, created.text
    step = await session_store.get_session(UUID(created.json()["session_id"]))
    case_id = UUID(contract_of()["case_id"])
    case_location = case_workspace(link_app.state.storage, link_app.state.settings, case_id).location("pack.docx")

    by_step = await office_sessions.open_or_reuse(
        session_factory, location=location_of(step, "pack.docx"), path="pack.docx", session_id=step.id,
    )
    by_person = await office_sessions.open_waiting(
        session_factory, location=case_location, path="pack.docx", session_id=None, case_id=case_id,
        user=("user:1", "Ana Pop"),
    )
    by_step_again = await office_sessions.open_or_reuse(
        session_factory, location=location_of(step, "pack.docx"), path="pack.docx", session_id=step.id,
    )

    assert by_person.id == by_step.id == by_step_again.id
    assert by_person.session_ids == (step.id,)
    assert by_person.case_id == case_id
    assert by_step_again.case_id == case_id
    await session_store.update_session_status(step.id, "archived")
    await _reap(session_factory, link_app.state.storage)
    assert await office_sessions.get_open(session_factory, by_step.id) is not None


async def test_a_case_document_never_opened_is_closed(session_factory, storage):
    document = await _open_for_case(session_factory, uuid.uuid4())

    await _reap(session_factory, storage)
    assert await office_sessions.get_open(session_factory, document.id) is not None

    await _reap(session_factory, storage, now=datetime.now(timezone.utc) + IDLE)
    assert await office_sessions.get_open(session_factory, document.id) is None


async def test_a_case_document_everyone_left_closes_once_idle(session_factory, storage):
    """The document server's close may never come: a case document nobody is in closes once it has been idle."""
    document = await _open_for_case(session_factory, uuid.uuid4())
    await office_sessions.record_editors(session_factory, document.id, ["user:1"])

    await _reap(session_factory, storage, now=datetime.now(timezone.utc) + IDLE)
    assert await office_sessions.get_open(session_factory, document.id) is not None

    await office_sessions.record_editors(session_factory, document.id, [])
    await _reap(session_factory, storage)
    assert await office_sessions.get_open(session_factory, document.id) is not None

    await _reap(session_factory, storage, now=datetime.now(timezone.utc) + IDLE)
    assert await office_sessions.get_open(session_factory, document.id) is None
