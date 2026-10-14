"""office_sessions: one open row per file, and what saves and closes do to it."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, update

from surogates.db.models import OfficeSession
from surogates.office import sessions as office_sessions

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    """Each test sees only its own rows: claims and the reaper scan the whole table."""
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


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
    chats = [uuid4() for _ in range(8)]
    opened = await asyncio.gather(*[
        office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=chat)
        for chat in chats
    ])
    assert len({document.doc_key for document in opened}) == 1
    assert set((await office_sessions.get_open(session_factory, opened[0].id)).session_ids) == set(chats)


async def test_closed_row_gives_a_new_key(session_factory):
    location = _location()
    one = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())
    assert await office_sessions.close(session_factory, one.id) is None
    assert await office_sessions.get_open(session_factory, one.id) is None
    two = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())
    assert two.doc_key != one.doc_key


async def test_reopening_a_closing_row_raises(session_factory):
    location = _location()
    document = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=True)
    with pytest.raises(office_sessions.DocumentClosing):
        await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())


async def test_newer_save_replaces_a_pending_one_and_keeps_close(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    older, newer = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc), datetime(2026, 10, 13, 9, 5, tzinfo=timezone.utc)
    assert await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k1", close=True, lastsave=older) is None
    replaced = await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k2", close=False, lastsave=newer)
    assert replaced == "k1"
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending == {
        "bucket": "b", "key": "k2", "lastsave": newer.isoformat(), "close": True, "attempts": 0,
        "marks": [], "authors": [], "changes": 0,
    }


async def test_older_save_arriving_late_keeps_the_newer_pending_one(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    older, newer = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc), datetime(2026, 10, 13, 9, 5, tzinfo=timezone.utc)
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="new", close=False, lastsave=newer)
    discarded = await office_sessions.set_pending(session_factory, document.id, bucket="b", key="old", close=True, lastsave=older)
    assert discarded == "old"
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending == {
        "bucket": "b", "key": "new", "lastsave": newer.isoformat(), "close": True, "attempts": 0,
        "marks": [], "authors": [], "changes": 0,
    }


async def test_a_save_counts_as_connected(session_factory):
    """A lost status-1 callback must not let the reaper close a row that receives saves."""
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    async with office_sessions.claim_pending(session_factory) as row:
        office_sessions.mark_landed(row, "etag-2")
    closed = await office_sessions.close_never_opened(
        session_factory, older_than=timedelta(minutes=10), now=datetime.now(timezone.utc) + timedelta(minutes=11),
    )
    assert closed == 0
    assert await office_sessions.get_open(session_factory, document.id) is not None


async def test_close_returns_the_dropped_pending_key(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    assert await office_sessions.close(session_factory, document.id) == "k"
    assert await office_sessions.get_open(session_factory, document.id) is None
    assert await office_sessions.close(session_factory, document.id) is None


async def test_close_waits_for_a_pending_save(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    await office_sessions.close_when_saved(session_factory, document.id)
    still = await office_sessions.get_open(session_factory, document.id)
    assert still is not None and still.pending_save["close"] is True
    async with office_sessions.claim_pending(session_factory) as row:
        assert row is not None and row.id == document.id
        office_sessions.mark_landed(row, "etag-2")
    assert await office_sessions.get_open(session_factory, document.id) is None


async def test_landing_records_the_new_version(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    async with office_sessions.claim_pending(session_factory) as row:
        office_sessions.mark_landed(row, "etag-9")
    after = await office_sessions.get_open(session_factory, document.id)
    assert after.base_version == "etag-9" and after.pending_save is None
    async with office_sessions.claim_pending(session_factory) as again:
        assert again is None  # SQL NULL, not a JSON null the next claim would pick up


async def test_each_claim_locks_one_row(session_factory):
    for path in ("a.docx", "b.docx"):
        document = await office_sessions.open_or_reuse(session_factory, location=_location(), path=path, session_id=uuid4())
        await office_sessions.set_pending(session_factory, document.id, bucket="b", key=path, close=False)
    async with office_sessions.claim_pending(session_factory) as first:
        async with office_sessions.claim_pending(session_factory) as second:
            async with office_sessions.claim_pending(session_factory) as third:
                assert first is not None and second is not None and first.id != second.id
                assert third is None


async def test_mark_attempt_counts_up(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    for expected in (1, 2):
        async with office_sessions.claim_pending(session_factory) as row:
            assert office_sessions.mark_attempt(row) == expected
    assert (await office_sessions.get_open(session_factory, document.id)).pending_save["attempts"] == 2


async def test_a_save_replacing_one_waiting_on_its_computer_records_its_write_abandoned(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k1", close=False)
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k2", close=False)
    assert "abandoned" not in (await office_sessions.get_open(session_factory, document.id)).pending_save
    async with office_sessions.claim_pending(session_factory) as row:
        row.pending_save = {**row.pending_save, "waiting": "waiting", "try": 2}
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k3", close=False)
    assert (await office_sessions.get_open(session_factory, document.id)).pending_save["abandoned"] == [{"key": "k2", "try": 2}]
    # Kept by the saves after it until a pass cancels it.
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k4", close=False)
    assert (await office_sessions.get_open(session_factory, document.id)).pending_save["abandoned"] == [{"key": "k2", "try": 2}]


async def test_leaving_a_waiting_try_records_its_request_abandoned(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    async with office_sessions.claim_pending(session_factory) as row:
        office_sessions.next_try(row)
        office_sessions.mark_waiting(row, "waiting")
        office_sessions.next_try(row)
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending["try"] == 2 and pending["abandoned"] == [{"key": "k", "try": 1}]


async def test_never_opened_rows_are_closed(session_factory):
    stale = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    seen = await office_sessions.open_or_reuse(session_factory, location=_location(), path="b.docx", session_id=uuid4())
    await office_sessions.record_editors(session_factory, seen.id, ["user:1"])
    closed = await office_sessions.close_never_opened(
        session_factory, older_than=timedelta(minutes=10), now=datetime.now(timezone.utc) + timedelta(minutes=11),
    )
    assert closed == 1
    assert await office_sessions.get_open(session_factory, stale.id) is None
    assert await office_sessions.get_open(session_factory, seen.id) is not None


async def test_a_reused_row_is_not_reaped(session_factory):
    location, chat = _location(), uuid4()
    document = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=chat)
    created = datetime.now(timezone.utc) - timedelta(minutes=15)
    async with session_factory() as db, db.begin():
        await db.execute(
            update(OfficeSession).where(OfficeSession.id == document.id).values(created_at=created, updated_at=created)
        )
    # The same chat again: nothing to add to the row, the clock still moves.
    await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=chat)
    closed = await office_sessions.close_never_opened(
        session_factory, older_than=timedelta(minutes=10), now=created + timedelta(minutes=11),
    )
    assert closed == 0
    assert await office_sessions.get_open(session_factory, document.id) is not None
