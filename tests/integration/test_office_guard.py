"""Writes to a document open in the editor are refused; reads are told the editor has the newest version."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import guard
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of
from surogates.session.store import SessionStore

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


@pytest.fixture(autouse=True)
def office_on(monkeypatch):
    monkeypatch.setattr(guard, "load_settings", lambda: SimpleNamespace(office=OFFICE))


async def _session_with_open(session_factory, path="docs/a.docx"):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await SessionStore(session_factory).create_session(
        user_id=user_id, org_id=org_id, agent_id="writer", channel="web", config={"storage_bucket": "b"},
    )
    await office_sessions.open_or_reuse(session_factory, location=location_of(session, path), path=path, session_id=session.id)
    return session


async def test_writing_an_open_document_is_refused(session_factory):
    session = await _session_with_open(session_factory)
    said = await guard.refused_open_document("write_file", {"path": "docs/a.docx", "content": "x"}, session,
                                             session_factory=session_factory)
    assert "office_edit" in said and "docs/a.docx" in said
    assert await guard.refused_open_document("patch", {"path": "/workspace/docs/a.docx", "old_string": "a", "new_string": "b"},
                                             session, session_factory=session_factory)
    for args in ({"path": "docs/b.docx", "content": "x"}, {"path": "notes.md", "content": "x"}):
        assert await guard.refused_open_document("write_file", args, session, session_factory=session_factory) is None
    assert await guard.refused_open_document("read_file", {"path": "docs/a.docx"}, session, session_factory=session_factory) is None


async def test_reading_an_open_document_is_noted(session_factory):
    session = await _session_with_open(session_factory)
    assert "office_read" in await guard.open_document_note({"path": "docs/a.docx"}, session, session_factory=session_factory)
    assert await guard.open_document_note({"path": "docs/b.docx"}, session, session_factory=session_factory) is None


async def test_a_threads_change_to_an_open_document_is_held_for_a_redo(session_factory):
    session = await _session_with_open(session_factory)
    turn = {
        "changes": [{"path": "docs/a.docx", "before": "r1", "after": "t1"}, {"path": "notes.md", "before": "r2", "after": "t2"}],
        "overlapped": [],
    }
    await guard.hold_open_documents(session_factory, session, turn)
    assert turn["changes"] == [{"path": "notes.md", "before": "r2", "after": "t2"}]
    assert turn["overlapped"] == [{"path": "docs/a.docx", "reason": "changed", "before": "r1", "after": "t1", "by": {"kind": "you"}}]
