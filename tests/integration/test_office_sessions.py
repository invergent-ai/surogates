"""office_sessions: one open row per file, and what saves and closes do to it."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from surogates.office import sessions as office_sessions

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _location() -> str:
    return f"bucket:test/{uuid4()}/a.docx"


async def test_same_location_reuses_the_key(session_factory):
    location = _location()
    first, second = uuid4(), uuid4()
    one = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=first)
    two = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=second)
    assert one.doc_key == two.doc_key
    assert set(two.session_ids) == {first, second}


async def test_concurrent_opens_get_one_key(session_factory):
    location = _location()
    opened = await asyncio.gather(*[
        office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())
        for _ in range(8)
    ])
    assert len({document.doc_key for document in opened}) == 1


async def test_closed_row_gives_a_new_key(session_factory):
    location = _location()
    one = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())
    await office_sessions.close(session_factory, one.id)
    assert await office_sessions.get_open(session_factory, one.id) is None
    two = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())
    assert two.doc_key != one.doc_key


async def test_newer_save_replaces_a_pending_one_and_keeps_close(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    assert await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k1", close=True) is None
    replaced = await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k2", close=False)
    assert replaced == "k1"
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending == {"bucket": "b", "key": "k2", "close": True, "attempts": 0}


async def test_close_waits_for_a_pending_save(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    await office_sessions.close_when_saved(session_factory, document.id)
    still = await office_sessions.get_open(session_factory, document.id)
    assert still is not None and still.pending_save["close"] is True
    async with office_sessions.claim_pending(session_factory) as rows:
        row = next(r for r in rows if r.id == document.id)
        office_sessions.mark_landed(row, "etag-2")
    assert await office_sessions.get_open(session_factory, document.id) is None


async def test_landing_records_the_new_version(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    async with office_sessions.claim_pending(session_factory) as rows:
        office_sessions.mark_landed(next(r for r in rows if r.id == document.id), "etag-9")
    after = await office_sessions.get_open(session_factory, document.id)
    assert after.base_version == "etag-9" and after.pending_save is None


async def test_never_opened_rows_are_closed(session_factory):
    stale = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    seen = await office_sessions.open_or_reuse(session_factory, location=_location(), path="b.docx", session_id=uuid4())
    await office_sessions.record_editors(session_factory, seen.id, ["user:1"])
    closed = await office_sessions.close_never_opened(
        session_factory, older_than=timedelta(minutes=10), now=datetime.now(timezone.utc) + timedelta(minutes=11),
    )
    assert closed >= 1
    assert await office_sessions.get_open(session_factory, stale.id) is None
    assert await office_sessions.get_open(session_factory, seen.id) is not None
