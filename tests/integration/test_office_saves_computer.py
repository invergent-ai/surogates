"""Landing the editor's saves in a folder on the user's computer."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.devices.binding import Binding
from surogates.devices.operations import DeviceOperations, TooManyRequests
from surogates.office import files as office_files
from surogates.office import saves as office_saves
from surogates.office import sessions as office_sessions
from surogates.office.files import EDITOR_REQUEST
from surogates.session.events import EventType
from surogates.session.files import ComputerAway
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from tests.device_computer import computer_io

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
)
FOLDER = "/home/me/notes"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


class Desk:
    """The computer as the save job reaches it: its files, and what each request asked."""

    def __init__(self, monkeypatch, files):
        self.io = computer_io(FOLDER, files, device_id=str(uuid4()))
        self.requests: list[dict] = []
        self.away: ComputerAway | None = None
        self.hold = False  # the computer answers nothing: the user has not answered its prompt
        self.deny = False

        @asynccontextmanager
        async def fake_session_files(session, **kwargs):
            self.requests.append(kwargs)
            if self.away is not None:
                raise self.away
            yield self.io

        async def bound(_db, _root):
            return Binding("bound")

        original_run = self.io.runner.run

        async def run(kind, args, payload=None):
            if kind == "write" and self.hold:
                await asyncio.sleep(3600)
            if kind == "write" and self.deny:
                return {"error": {"type": "os", "code": "EACCES", "message": "The user denied this change on this computer"}}
            return await original_run(kind, args, payload)

        self.io.runner.run = run
        monkeypatch.setattr(office_files, "session_files", fake_session_files)
        monkeypatch.setattr(office_files, "binding_of", bound)
        monkeypatch.setattr(office_saves, "COMPUTER_WAIT_S", 0.2)


async def _computer_document(session_factory, storage, desk, path="a.docx", history=None, boundary=None):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    device_id = desk.io.identity.split(":", 1)[1]
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="default", channel="web", config={
        "storage_bucket": "b", "workspace_path": FOLDER,
        "execution": {"kind": "device", "device_id": device_id, "device_name": "Mac",
                      **({"history": history} if history else {})},
        **({"workspace_boundary": boundary} if boundary else {}),
    })
    key = f"{FOLDER}/{path}"
    document = await office_sessions.open_or_reuse(
        session_factory, location=f"device:{device_id}:{key}", path=path, session_id=session.id,
    )
    await office_sessions.record_base_version(session_factory, document.id, desk.io.runner.files[key][1])
    return store, session, key, document


async def _pend(session_factory, storage, document, data, *, close=False, lastsave=None):
    pending_key = f"_office/pending/{document.doc_key}/{uuid4().hex}"
    await storage.write("b", pending_key, data)
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key=pending_key, close=close,
                                      lastsave=lastsave)
    return pending_key


def _answer_writes(desk, error):
    """The computer answers every write with *error*; its own runner, to put back."""
    original = desk.io.runner.run

    async def run(kind, args, payload=None):
        if kind == "write":
            return {"error": error}
        return await original(kind, args, payload)

    desk.io.runner.run = run
    return original


async def _nothing(*_args, **_kwargs):
    return None


async def _land(session_factory, redis_client, storage, store):
    return await office_saves.land_pending_saves(session_factory=session_factory, session_store=store,
                                                 storage=storage, redis=redis_client, office=OFFICE)


async def _types(store, session_id):
    return [event.type for event in await store.get_events(session_id)]


async def test_a_save_lands_on_the_computer_over_the_revision_it_opened(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, session, key, document = await _computer_document(session_factory, storage, desk)
    pending_key = await _pend(session_factory, storage, document, b"v2")
    assert await _land(session_factory, redis_client, storage, store) == 1
    assert desk.io.runner.files[key][0] == b"v2"
    [request] = desk.requests
    assert request["request_id"].startswith(f"{EDITOR_REQUEST}{document.doc_key}.")
    assert request["access"].session_id == session.id
    still_open = await office_sessions.get_open(session_factory, document.id)
    assert still_open.pending_save is None and still_open.base_version == desk.io.runner.files[key][1]
    assert not await storage.exists("b", pending_key)


async def test_a_save_waits_for_an_offline_computer_without_failing(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, session, key, document = await _computer_document(session_factory, storage, desk)
    await _pend(session_factory, storage, document, b"v2")
    desk.away = ComputerAway("Mac", revoked=False)
    for _ in range(office_saves.SAVE_FAILED_AFTER + 2):
        assert await _land(session_factory, redis_client, storage, store) == 0
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending["attempts"] == 0 and pending["waiting"] == "offline"
    types = await _types(store, session.id)
    assert types.count(EventType.OFFICE_SAVE_WAITING.value) == 1
    assert EventType.OFFICE_SAVE_FAILED.value not in types
    desk.away = None
    assert await _land(session_factory, redis_client, storage, store) == 1
    assert desk.io.runner.files[key][0] == b"v2"
    assert EventType.OFFICE_SAVED.value in await _types(store, session.id)


async def test_a_save_waiting_for_its_users_ok_joins_the_same_request_again(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, session, _, document = await _computer_document(session_factory, storage, desk)
    await _pend(session_factory, storage, document, b"v2")
    desk.hold = True
    assert await _land(session_factory, redis_client, storage, store) == 0
    assert await _land(session_factory, redis_client, storage, store) == 0
    first, second = desk.requests
    assert first["request_id"] == second["request_id"] and first["change"] == second["change"]
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending["waiting"] == "waiting" and pending["attempts"] == 0
    assert (await _types(store, session.id)).count(EventType.OFFICE_SAVE_WAITING.value) == 1


async def test_too_many_open_requests_is_waiting_not_a_failed_try(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, _, _, document = await _computer_document(session_factory, storage, desk)
    await _pend(session_factory, storage, document, b"v2")
    desk.away = TooManyRequests("too many of your changes are open on this computer")
    assert await _land(session_factory, redis_client, storage, store) == 0
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending["waiting"] == "waiting" and pending["attempts"] == 0


async def test_a_pass_cancels_the_writes_left_open_by_the_saves_it_replaced(session_factory, redis_client, storage, monkeypatch):
    cancelled: list = []

    async def cancel_invocation(_self, calling_session_id, invocation_id):
        cancelled.append((calling_session_id, invocation_id))
        return 1

    monkeypatch.setattr(DeviceOperations, "cancel_invocation", cancel_invocation)
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, session, key, document = await _computer_document(session_factory, storage, desk)
    first = await _pend(session_factory, storage, document, b"v2")
    desk.hold = True
    assert await _land(session_factory, redis_client, storage, store) == 0
    await _pend(session_factory, storage, document, b"v3")
    assert await _land(session_factory, redis_client, storage, store) == 0
    assert cancelled == [(session.id, f"request:editor.{document.doc_key}.{PurePosixPath(first).name}.0")]
    assert "abandoned" not in (await office_sessions.get_open(session_factory, document.id)).pending_save
    desk.hold = False
    assert await _land(session_factory, redis_client, storage, store) == 1
    assert len(cancelled) == 1 and desk.io.runner.files[key][0] == b"v3"


async def test_a_declined_save_is_dropped_and_said(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, session, key, document = await _computer_document(session_factory, storage, desk)
    pending_key = await _pend(session_factory, storage, document, b"v2")
    desk.deny = True
    assert await _land(session_factory, redis_client, storage, store) == 0
    assert desk.io.runner.files[key][0] == b"v1"
    still_open = await office_sessions.get_open(session_factory, document.id)
    assert still_open is not None and still_open.pending_save is None
    assert not await storage.exists("b", pending_key)
    [failed] = [e for e in await store.get_events(session.id) if e.type == EventType.OFFICE_SAVE_FAILED.value]
    assert failed.data == {"path": "a.docx", "reason": "declined", "computer": "Mac"}


async def test_a_declined_closing_save_closes_its_document(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, _, _, document = await _computer_document(session_factory, storage, desk)
    pending_key = await _pend(session_factory, storage, document, b"v2", close=True)
    desk.deny = True
    assert await _land(session_factory, redis_client, storage, store) == 0
    assert await office_sessions.get_open(session_factory, document.id) is None
    assert not await storage.exists("b", pending_key)


@pytest.mark.parametrize("failure", [
    {"type": "os", "code": "EACCES", "message": "Nobody answered on this computer in time, so the change was not made"},
    {"type": "os", "code": "ETIMEDOUT", "message": "Operation timed out"},
    "revoked",
])
async def test_a_failure_short_of_a_denial_is_retried_never_dropped(session_factory, redis_client, storage, monkeypatch,
                                                                    failure):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, _, _, document = await _computer_document(session_factory, storage, desk)
    # A closing save: the last editor has left, so dropping it would lose the edits.
    pending_key = await _pend(session_factory, storage, document, b"v2", close=True)
    if failure == "revoked":
        desk.away = ComputerAway("Mac", revoked=True)
    else:
        _answer_writes(desk, failure)
    for _ in range(2):
        assert await _land(session_factory, redis_client, storage, store) == 0
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending["key"] == pending_key and pending["attempts"] == 2 and "waiting" not in pending
    assert await storage.exists("b", pending_key)
    first, second = desk.requests
    assert first["request_id"] != second["request_id"]


async def test_a_failed_save_asks_the_computer_anew_next_time(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, _, _, document = await _computer_document(session_factory, storage, desk)
    await _pend(session_factory, storage, document, b"v2")
    original = _answer_writes(desk, {"type": "unavailable", "message": "another chat on this computer is working in this folder"})
    assert await _land(session_factory, redis_client, storage, store) == 0
    desk.io.runner.run = original
    assert await _land(session_factory, redis_client, storage, store) == 1
    first, second = desk.requests
    assert first["request_id"] != second["request_id"]


async def test_a_changed_file_on_the_computer_lands_as_a_copy_beside_it(session_factory, redis_client, storage, monkeypatch):
    async def no_drop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(office_saves, "drop_editors", no_drop)
    monkeypatch.setattr(office_saves, "close_pages", no_drop)
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, _, key, document = await _computer_document(session_factory, storage, desk)
    await desk.io.write(key, b"someone else")
    await _pend(session_factory, storage, document, b"mine")
    await _land(session_factory, redis_client, storage, store)
    assert desk.io.runner.files[key][0] == b"someone else"
    copies = [k for k in desk.io.runner.files if k.startswith(f"{FOLDER}/a (edited ")]
    assert len(copies) == 1 and desk.io.runner.files[copies[0]][0] == b"mine"


async def test_a_conflict_copy_is_named_by_the_saves_own_time(session_factory, redis_client, storage, monkeypatch):
    monkeypatch.setattr(office_saves, "drop_editors", _nothing)
    monkeypatch.setattr(office_saves, "close_pages", _nothing)
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, _, key, document = await _computer_document(session_factory, storage, desk)
    await desk.io.write(key, b"someone else")
    await _pend(session_factory, storage, document, b"mine", lastsave=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc))
    await _land(session_factory, redis_client, storage, store)
    assert desk.io.runner.files[f"{FOLDER}/a (edited 2026-01-02 03.04 UTC).docx"][0] == b"mine"


async def test_a_project_threads_save_is_a_plain_write_through_its_master(session_factory, redis_client, storage,
                                                                          monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    project_id = uuid4()
    store, thread, key, document = await _computer_document(
        session_factory, storage, desk, history={"thread": str(uuid4())}, boundary=f"workstream:{project_id}",
    )
    master = await store.create_session(user_id=thread.user_id, org_id=thread.org_id, agent_id="default", channel="web",
                                        config={**thread.config, "execution": {**thread.config["execution"], "history": None}})

    async def master_of(_factory, pid):
        assert pid == project_id
        return master

    async def project_of(_factory, _session):
        return SimpleNamespace(id=project_id, user_id=thread.user_id)

    async def land_editor_save(*_args, **_kwargs):
        raise AssertionError("a save on a computer is no history landing")

    monkeypatch.setattr(office_files, "_master_of", master_of)
    monkeypatch.setattr(office_saves, "_project_of", project_of)
    monkeypatch.setattr(office_saves, "land_editor_save", land_editor_save)
    await _pend(session_factory, storage, document, b"v2")
    assert await _land(session_factory, redis_client, storage, store) == 1
    assert desk.io.runner.files[key][0] == b"v2"
    [request] = desk.requests
    assert request["access"].session_id == master.id
