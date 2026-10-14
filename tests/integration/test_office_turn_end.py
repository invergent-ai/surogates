"""The turn's save: flush, forcesave with the turn's mark, wait for the callback that carries it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import sessions as office_sessions
from surogates.office import turn_end
from surogates.office.agent import location_of
from surogates.office.runtime_office import PageGone
from surogates.session.events import EventType
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


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


async def _turn(session_factory, storage):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="writer", channel="web",
                                         config={"storage_bucket": "b"})
    await storage.write("b", session_workspace_key(session.id, "a.docx"), b"PK-docx")
    document = await office_sessions.open_or_reuse(session_factory, location=location_of(session, "a.docx"),
                                                   path="a.docx", session_id=session.id)
    after = await store.last_event_id(session.id)
    for name, path in (("office_read", "a.docx"), ("office_edit", "__WORKSPACE__/a.docx"), ("office_edit", "a.docx")):
        await store.emit_event(session.id, EventType.TOOL_CALL, {"tool_call_id": name, "name": name, "arguments": {"path": path}})
    return store, session, document, after


async def test_the_turn_names_each_edited_document_once(session_factory, storage):
    store, session, _, after = await _turn(session_factory, storage)
    assert await turn_end.edited_paths(store, session.id, after) == ["__WORKSPACE__/a.docx", "a.docx"]


async def test_the_turns_save_flushes_then_forces_a_save_and_waits_for_its_mark(session_factory, storage, monkeypatch):
    store, session, document, after = await _turn(session_factory, storage)
    flushed: list[str] = []
    sent: list[str] = []

    class Runtime:
        def __init__(self, office):
            pass

        async def flush(self, page):
            flushed.append(page)
            return True

    async def force_save(office, doc_key, userdata):
        sent.append(userdata)

        async def callback():
            await asyncio.sleep(0.2)
            await office_sessions.set_pending(session_factory, document.id, bucket="b", key="_office/pending/x",
                                              close=False, marks=[userdata], authors=["agent:writer:s"])
        asyncio.get_running_loop().create_task(callback())
        return 0

    monkeypatch.setattr(turn_end, "RuntimeOffice", Runtime)
    monkeypatch.setattr(turn_end, "force_save", force_save)
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE,
                             session_factory=session_factory)
    assert flushed == [f"{document.doc_key}.{session.id}"]
    assert len(sent) == 1 and json.loads(sent[0])["turn"]
    assert (await office_sessions.get_open(session_factory, document.id)).pending_save["marks"] == sent


async def test_a_closed_page_still_gets_its_save_and_nothing_unsaved_ends_at_once(session_factory, storage, monkeypatch):
    store, session, _, after = await _turn(session_factory, storage)
    sent: list[str] = []

    class Gone:
        def __init__(self, office):
            pass

        async def flush(self, page):
            raise PageGone("closed", 410)

    async def nothing_unsaved(office, doc_key, userdata):
        sent.append(userdata)
        return 4

    monkeypatch.setattr(turn_end, "force_save", nothing_unsaved)
    monkeypatch.setattr(turn_end, "RuntimeOffice", Gone)
    started = time.monotonic()
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE,
                             session_factory=session_factory)
    assert len(sent) == 1
    assert time.monotonic() - started < 5


def _flushing(result: bool):
    class Runtime:
        def __init__(self, office):
            pass

        async def flush(self, page):
            return result

    return Runtime


async def test_the_turns_slots_go_back_only_for_a_turn_that_edited(session_factory, storage, monkeypatch):
    store, session, _, after = await _turn(session_factory, storage)
    entered: list[bool] = []

    @contextlib.asynccontextmanager
    async def recording_wait():
        entered.append(True)
        yield

    async def nothing_unsaved(office, doc_key, userdata):
        return 4

    monkeypatch.setattr(turn_end, "turn_waiting", recording_wait)
    monkeypatch.setattr(turn_end, "RuntimeOffice", _flushing(True))
    monkeypatch.setattr(turn_end, "force_save", nothing_unsaved)
    edited_none = await store.last_event_id(session.id)
    await turn_end.save_turn(store=store, session=session, after=edited_none, office=OFFICE,
                             session_factory=session_factory)
    assert entered == []
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE,
                             session_factory=session_factory)
    assert entered == [True]


async def test_an_editor_with_unsent_changes_gets_no_forced_save(session_factory, storage, monkeypatch):
    store, session, _, after = await _turn(session_factory, storage)
    sent: list[str] = []

    async def force_save(office, doc_key, userdata):
        sent.append(userdata)
        return 0

    monkeypatch.setattr(turn_end, "RuntimeOffice", _flushing(False))
    monkeypatch.setattr(turn_end, "force_save", force_save)
    started = time.monotonic()
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE,
                             session_factory=session_factory)
    assert sent == []
    assert time.monotonic() - started < 5


async def test_a_turn_on_the_computer_saves_the_document_it_edited(session_factory, monkeypatch):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="writer", channel="web", config={
        "storage_bucket": "b", "workspace_path": "/home/me/notes",
        "execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"},
    })
    document = await office_sessions.open_or_reuse(
        session_factory, location=location_of(session, "a.docx"), path="a.docx", session_id=session.id,
    )
    after = (await store.get_events(session.id))[-1].id if await store.get_events(session.id) else 0
    await store.emit_event(session.id, EventType.TOOL_CALL, {"name": "office_edit", "arguments": {"path": "a.docx", "ops": []}})
    flushed, sent = [], []

    class Runtime:
        def __init__(self, office):
            pass

        async def flush(self, page):
            flushed.append(page)
            return True

    async def force_save(office, key, userdata):
        sent.append(userdata)
        return 4  # nothing unsaved: every edit is saved

    monkeypatch.setattr(turn_end, "RuntimeOffice", Runtime)
    monkeypatch.setattr(turn_end, "force_save", force_save)
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE, session_factory=session_factory)
    assert flushed == [f"{document.doc_key}.{session.id}"] and len(sent) == 1


async def test_a_refused_path_saves_nothing_and_stops_no_other_save(session_factory, storage, monkeypatch):
    store, session, document, _ = await _turn(session_factory, storage)
    after = await store.last_event_id(session.id)
    for path in ("../b.docx", "a.docx"):  # a traversal office_edit refused, then the document
        await store.emit_event(session.id, EventType.TOOL_CALL, {"name": "office_edit", "arguments": {"path": path}})
    saved: list[str] = []

    async def recording_save(runtime, office, session_factory, opened, session_id):
        saved.append(opened.location)

    monkeypatch.setattr(turn_end, "_save", recording_save)
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE, session_factory=session_factory)
    assert saved == [document.location]


async def test_a_path_outside_the_folder_on_the_computer_saves_nothing(session_factory, monkeypatch):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    device = "00000000-0000-0000-0000-000000000001"
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="writer", channel="web", config={
        "storage_bucket": "b", "workspace_path": "/home/me/notes", "execution": {"kind": "device", "device_id": device},
    })
    # Another chat's document on the same computer, open in the editor.
    other = "/Users/me/other/b.docx"
    await office_sessions.open_or_reuse(session_factory, location=f"device:{device}:{other}", path="b.docx", session_id=session.id)
    after = await store.last_event_id(session.id)
    await store.emit_event(session.id, EventType.TOOL_CALL, {"name": "office_edit", "arguments": {"path": other}})
    saved: list[str] = []

    async def recording_save(runtime, office, session_factory, opened, session_id):
        saved.append(opened.location)

    monkeypatch.setattr(turn_end, "_save", recording_save)
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE, session_factory=session_factory)
    assert saved == []
