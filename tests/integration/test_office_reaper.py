"""Closing editing sessions nobody uses: never opened, or whose chats are gone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import reaper
from surogates.office import sessions as office_sessions
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(public_url="https://o", internal_url="http://d", callback_base_url="https://a",
                        jwt_secret="j" * 32, token_secret="t" * 32)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    """Each test sees only its own rows: the reaper scans the whole table."""
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


@pytest.fixture
def dropped(monkeypatch):
    calls = []

    async def fake_drop(office, doc_key, users):
        calls.append(doc_key)

    monkeypatch.setattr(reaper, "drop_editors", fake_drop)
    return calls


async def _archived_chat_with_document(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="default", channel="web", config={})
    document = await office_sessions.open_or_reuse(session_factory, location=f"bucket:b/{uuid4()}", path="a.docx", session_id=session.id)
    await office_sessions.record_editors(session_factory, document.id, ["user:1"])
    await store.update_session_status(session.id, "archived")
    return store, document


async def test_documents_of_archived_chats_are_dropped_and_closed(session_factory, storage, dropped):
    store, document = await _archived_chat_with_document(session_factory)
    closed = await reaper.reap(session_factory=session_factory, session_store=store, office=OFFICE, storage=storage)
    assert closed == 1
    assert document.doc_key in dropped
    assert await office_sessions.get_open(session_factory, document.id) is None


async def test_closing_an_archived_chats_document_deletes_its_pending_save(session_factory, storage, dropped):
    store, document = await _archived_chat_with_document(session_factory)
    pending_key = f"_office/pending/{document.doc_key}/{uuid4().hex}"
    await storage.write("b", pending_key, b"v2")
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key=pending_key, close=False)
    await reaper.reap(session_factory=session_factory, session_store=store, office=OFFICE, storage=storage)
    assert await office_sessions.get_open(session_factory, document.id) is None
    assert not await storage.exists("b", pending_key)


async def test_documents_of_live_chats_stay_open(session_factory, storage, dropped):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="default", channel="web", config={})
    document = await office_sessions.open_or_reuse(session_factory, location=f"bucket:b/{uuid4()}", path="a.docx", session_id=session.id)
    await office_sessions.record_editors(session_factory, document.id, ["user:1"])
    assert await reaper.reap(session_factory=session_factory, session_store=store, office=OFFICE, storage=storage) == 0
    assert dropped == []
    assert await office_sessions.get_open(session_factory, document.id) is not None


async def test_never_opened_documents_close_after_ten_minutes(session_factory, storage):
    store = SessionStore(session_factory)
    document = await office_sessions.open_or_reuse(session_factory, location=f"bucket:b/{uuid4()}", path="a.docx", session_id=uuid4())
    await reaper.reap(session_factory=session_factory, session_store=store, office=OFFICE, storage=storage,
                      now=datetime.now(timezone.utc) + timedelta(minutes=11))
    assert await office_sessions.get_open(session_factory, document.id) is None
