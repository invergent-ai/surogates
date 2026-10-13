"""Landing pending saves: guarded writes, conflict copies, closing."""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import saves as office_saves
from surogates.office import sessions as office_sessions
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import session_workspace_key

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    """Each test sees only its own rows: a landing pass claims across the whole table."""
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


async def _chat_with_document(session_factory, storage, content=b"v1", path="a.docx"):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="default", channel="web",
                                         config={"storage_bucket": "b"})
    key = session_workspace_key(session.id, path)
    await storage.write("b", key, content)
    document = await office_sessions.open_or_reuse(session_factory, location=f"bucket:b/{key}", path=path, session_id=session.id)
    await office_sessions.record_base_version(session_factory, document.id, (await storage.stat("b", key))["etag"])
    return store, session, key, document


async def _pend(session_factory, storage, document, data, *, close=False):
    pending_key = f"_office/pending/{document.doc_key}/{uuid4().hex}"
    if data is not None:
        await storage.write("b", pending_key, data)
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key=pending_key, close=close)
    return pending_key


async def _nothing_pending(session_factory):
    async with office_sessions.claim_pending(session_factory) as row:
        return row is None


async def _land(session_factory, redis_client, storage, store):
    return await office_saves.land_pending_saves(session_factory=session_factory, session_store=store,
                                                 storage=storage, redis=redis_client, office=OFFICE)


async def test_save_lands_and_closes(session_factory, redis_client, storage):
    store, _, key, document = await _chat_with_document(session_factory, storage)
    pending_key = await _pend(session_factory, storage, document, b"v2", close=True)
    assert await _land(session_factory, redis_client, storage, store) == 1
    assert await storage.read("b", key) == b"v2"
    assert not await storage.exists("b", pending_key)
    assert await office_sessions.get_open(session_factory, document.id) is None


async def test_save_lands_and_stays_open_on_the_new_version(session_factory, redis_client, storage):
    store, _, key, document = await _chat_with_document(session_factory, storage)
    await _pend(session_factory, storage, document, b"v2")
    assert await _land(session_factory, redis_client, storage, store) == 1
    still_open = await office_sessions.get_open(session_factory, document.id)
    assert still_open.pending_save is None
    assert still_open.base_version == (await storage.stat("b", key))["etag"]
    assert await _nothing_pending(session_factory)


async def test_changed_file_lands_as_a_sibling_copy(session_factory, redis_client, storage, monkeypatch):
    dropped: list[tuple[str, list[str]]] = []

    async def fake_drop(office, doc_key, users):
        dropped.append((doc_key, users))

    monkeypatch.setattr(office_saves, "drop_editors", fake_drop)
    store, session, key, document = await _chat_with_document(session_factory, storage)
    await office_sessions.record_editors(session_factory, document.id, ["user:1"])
    await storage.write("b", key, b"someone else")
    pending_key = await _pend(session_factory, storage, document, b"mine")
    await _land(session_factory, redis_client, storage, store)
    assert await storage.read("b", key) == b"someone else"
    # LocalBackend lists a folder, not a partial name: filter the workspace's listing.
    copies = [entry for entry in await storage.list_entries("b", prefix=session_workspace_key(session.id))
              if entry["key"].startswith(session_workspace_key(session.id, "a (edited "))]
    assert len(copies) == 1
    assert await storage.read("b", copies[0]["key"]) == b"mine"
    assert not await storage.exists("b", pending_key)
    assert dropped == [(document.doc_key, ["user:1"])]
    assert await office_sessions.get_open(session_factory, document.id) is None
    events = await store.get_events(session.id)
    assert any(e.type == "office.conflict" for e in events)


async def test_chat_gone_closes_without_writing(session_factory, redis_client, storage):
    store, session, key, document = await _chat_with_document(session_factory, storage)
    pending_key = await _pend(session_factory, storage, document, b"v2")
    await store.update_session_status(session.id, "archived")
    await _land(session_factory, redis_client, storage, store)
    assert await storage.read("b", key) == b"v1"
    assert not await storage.exists("b", pending_key)
    assert await office_sessions.get_open(session_factory, document.id) is None
    assert await _nothing_pending(session_factory)


async def test_one_failing_row_does_not_stop_the_others(session_factory, redis_client, storage):
    store, _, _, broken = await _chat_with_document(session_factory, storage, path="broken.docx")
    await _pend(session_factory, storage, broken, None)  # its pending blob is missing
    _, _, key, document = await _chat_with_document(session_factory, storage)
    await _pend(session_factory, storage, document, b"v2")
    assert await _land(session_factory, redis_client, storage, store) == 1
    assert await storage.read("b", key) == b"v2"
    retried = await office_sessions.get_open(session_factory, broken.id)
    assert retried.pending_save["attempts"] == 1  # tried once this pass, not again and again


async def test_a_save_already_written_lands_without_a_copy(session_factory, redis_client, storage, monkeypatch):
    """A landing whose write went through but whose row change never committed (the worker stopped)."""
    dropped: list[str] = []

    async def fake_drop(office, doc_key, users):
        dropped.append(doc_key)

    monkeypatch.setattr(office_saves, "drop_editors", fake_drop)
    store, session, key, document = await _chat_with_document(session_factory, storage)
    await office_sessions.record_editors(session_factory, document.id, ["user:1"])
    await storage.write("b", key, b"v2")  # the earlier landing's write
    await _pend(session_factory, storage, document, b"v2")
    assert await _land(session_factory, redis_client, storage, store) == 1
    landed = await office_sessions.get_open(session_factory, document.id)
    assert landed is not None and landed.pending_save is None
    assert landed.base_version == (await storage.stat("b", key))["etag"]
    copies = [entry for entry in await storage.list_entries("b", prefix=session_workspace_key(session.id))
              if entry["key"].startswith(session_workspace_key(session.id, "a (edited "))]
    assert copies == []
    assert dropped == []
    assert not any(e.type == "office.conflict" for e in await store.get_events(session.id))
