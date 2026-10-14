"""Documents open in the document server: one ``office_sessions`` row per open file."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from surogates.db.models import OfficeSession

OPEN = "open"
CLOSED = "closed"
# How long an open waits for a closing document's last save to land, and how often it looks.
CLOSING_WAIT_S = 10.0
CLOSING_POLL_S = 0.5


@dataclass(frozen=True, slots=True)
class OpenDocument:
    id: UUID
    doc_key: str
    path: str
    location: str
    session_ids: tuple[UUID, ...]
    base_version: str | None
    editors: tuple[str, ...]
    pending_save: dict[str, Any] | None
    user_names: dict[str, str]
    changes_seen: int
    connected_at: datetime | None


def _snapshot(row: OfficeSession) -> OpenDocument:
    return OpenDocument(
        id=row.id,
        doc_key=row.doc_key,
        path=row.path,
        location=row.location,
        session_ids=tuple(UUID(str(value)) for value in row.session_ids),
        base_version=row.base_version,
        editors=tuple(str(value) for value in row.editors),
        pending_save=dict(row.pending_save) if row.pending_save is not None else None,
        user_names={str(key): str(value) for key, value in (row.user_names or {}).items()},
        changes_seen=row.changes_seen or 0,
        connected_at=row.connected_at,
    )


class DocumentClosing(Exception):
    """The file's open row only waits for its last save to land; open the file again once it has."""


async def open_or_reuse(
    session_factory: Any, *, location: str, path: str, session_id: UUID, user: tuple[str, str] | None = None,
) -> OpenDocument:
    """The open row for *location*, created when there is none; *session_id* joins its chats,
    and *user* (editor user id, name) its names.

    Raises :class:`DocumentClosing` for a row whose pending save closes it: the document
    server ended that key's session, so edits made under the key would be lost.
    """
    async with session_factory() as db, db.begin():
        row = await _insert_and_lock(db, location=location, path=path, session_id=session_id)
        if row is None:  # closed between the insert and the select: insert again
            row = await _insert_and_lock(db, location=location, path=path, session_id=session_id)
        if row is None or (row.pending_save or {}).get("close"):
            raise DocumentClosing(location)
        if str(session_id) not in row.session_ids:
            row.session_ids = [*row.session_ids, str(session_id)]
        if user is not None and (row.user_names or {}).get(user[0]) != user[1]:
            row.user_names = {**(row.user_names or {}), user[0]: user[1]}
        # Every open moves the clock close_never_opened measures from.
        row.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return _snapshot(row)


async def open_waiting(
    session_factory: Any, *, location: str, path: str, session_id: UUID, user: tuple[str, str] | None = None,
) -> OpenDocument:
    """:func:`open_or_reuse`, waiting up to CLOSING_WAIT_S for a closing row's last save to land;
    raises :class:`DocumentClosing` once the wait is over."""
    deadline = time.monotonic() + CLOSING_WAIT_S
    while True:
        try:
            return await open_or_reuse(session_factory, location=location, path=path, session_id=session_id, user=user)
        except DocumentClosing:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(CLOSING_POLL_S)


async def _insert_and_lock(db: Any, *, location: str, path: str, session_id: UUID) -> OfficeSession | None:
    """Insert an open row for *location* unless one is open, then lock the open row (None if it closed meanwhile)."""
    await db.execute(
        pg_insert(OfficeSession)
        .values(
            id=uuid4(), path=path, location=location, session_ids=[str(session_id)],
            doc_key=uuid4().hex, status=OPEN,
        )
        .on_conflict_do_nothing(index_elements=["location"], index_where=text("status = 'open'"))
    )
    return await db.scalar(
        select(OfficeSession)
        .where(OfficeSession.location == location, OfficeSession.status == OPEN)
        .with_for_update()
    )


async def get_open(session_factory: Any, row_id: UUID) -> OpenDocument | None:
    async with session_factory() as db:
        row = await db.scalar(select(OfficeSession).where(OfficeSession.id == row_id, OfficeSession.status == OPEN))
        return _snapshot(row) if row is not None else None


async def open_at(session_factory: Any, location: str) -> OpenDocument | None:
    """The open row of the file at *location*, or None."""
    async with session_factory() as db:
        row = await db.scalar(select(OfficeSession).where(OfficeSession.location == location, OfficeSession.status == OPEN))
        return _snapshot(row) if row is not None else None


async def open_locations(session_factory: Any, locations: list[str]) -> set[str]:
    """Those of *locations* whose file is open in the editor."""
    if not locations:
        return set()
    async with session_factory() as db:
        found = await db.scalars(
            select(OfficeSession.location).where(OfficeSession.location.in_(locations), OfficeSession.status == OPEN)
        )
        return set(found.all())


async def open_documents(session_factory: Any) -> list[OpenDocument]:
    async with session_factory() as db:
        rows = (await db.scalars(select(OfficeSession).where(OfficeSession.status == OPEN))).all()
        return [_snapshot(row) for row in rows]


async def record_base_version(session_factory: Any, row_id: UUID, version: str | None) -> None:
    async with session_factory() as db, db.begin():
        await db.execute(update(OfficeSession).where(OfficeSession.id == row_id).values(base_version=version))


async def record_editors(session_factory: Any, row_id: UUID, users: list[str]) -> None:
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is None:
            return
        row.editors = list(users)
        if users and row.connected_at is None:
            row.connected_at = datetime.now(timezone.utc)


async def set_pending(
    session_factory: Any, row_id: UUID, *, bucket: str, key: str, close: bool, lastsave: datetime | None = None,
    marks: list[str] | None = None, authors: list[str] | None = None, changes: int | None = None,
) -> str | None:
    """Record a save waiting to land, made at *lastsave* (now when None); returns the key
    of the save that lost, to delete.

    Callbacks can arrive out of order (a slow download, autoAssembly racing a Save), so a
    pending save made later than *lastsave* stays and the incoming *key* is the one that
    lost; either way a ``close`` from either save survives.
    """
    lastsave = lastsave or datetime.now(timezone.utc)
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is None:
            return None
        # A save means an editor connected, though its status-1 callback may have been lost.
        if row.connected_at is None:
            row.connected_at = datetime.now(timezone.utc)
        previous = dict(row.pending_save or {})
        close = close or bool(previous.get("close"))
        # The newer save holds both saves' edits: everyone who made either, and every forced save either answers.
        marks = _union(previous.get("marks"), marks)
        authors = _union(previous.get("authors"), authors)
        changes = max(int(previous.get("changes") or 0), int(changes or 0))
        if previous and datetime.fromisoformat(previous["lastsave"]) > lastsave:
            row.pending_save = {**previous, "close": close, "marks": marks, "authors": authors, "changes": changes}
            return key
        pending: dict[str, Any] = {
            "bucket": bucket,
            "key": key,
            "lastsave": lastsave.isoformat(),
            "close": close,
            "attempts": 0,
            "marks": marks,
            "authors": authors,
            "changes": changes,
        }
        if previous.get("waiting"):
            # The chats were told the save waits: still so until this one lands, which says it.
            pending["waiting"] = previous["waiting"]
        abandoned = list(previous.get("abandoned") or [])
        if previous.get("waiting") == "waiting":
            # Its write may still be open on the computer: landing later, it would make this save a conflict copy.
            abandoned.append({"key": previous["key"], "try": int(previous.get("try", 0))})
        if abandoned:
            pending["abandoned"] = abandoned
        row.pending_save = pending
        replaced = previous.get("key")
        return str(replaced) if replaced and replaced != key else None


def _union(first: Any, second: Any) -> list[str]:
    out: list[str] = []
    for value in [*(first or []), *(second or [])]:
        if str(value) not in out:
            out.append(str(value))
    return out


async def close_when_saved(session_factory: Any, row_id: UUID) -> None:
    """Close the row now, or once its pending save lands."""
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is None or row.status != OPEN:
            return
        if row.pending_save is None:
            _close(row)
        else:
            row.pending_save = {**row.pending_save, "close": True}


async def close(session_factory: Any, row_id: UUID) -> str | None:
    """Close the row now, dropping any pending save; returns the dropped save's key."""
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is None or row.status != OPEN:
            return None
        dropped = (row.pending_save or {}).get("key")
        row.pending_save = None
        _close(row)
        return str(dropped) if dropped else None


@asynccontextmanager
async def claim_pending(session_factory: Any) -> AsyncIterator[OfficeSession | None]:
    """The oldest row with a save to land (None when there is none), locked so no other worker lands it.

    One row per transaction, so a failed landing never undoes another and a callback
    waits on one row at most. The caller changes the row (``mark_*``), the change
    commits when the block ends, and the caller claims again for the next row.
    """
    async with session_factory() as db, db.begin():
        yield await db.scalar(
            select(OfficeSession)
            .where(OfficeSession.pending_save.is_not(None))
            .order_by(OfficeSession.updated_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )


def mark_landed(row: OfficeSession, version: str | None) -> None:
    pending = row.pending_save or {}
    row.base_version = version
    # The changes this save covered are history now: the next save's authors start after them.
    row.changes_seen = max(row.changes_seen or 0, int(pending.get("changes") or 0))
    row.pending_save = None
    if pending.get("close"):
        _close(row)


def mark_closed(row: OfficeSession) -> None:
    row.pending_save = None
    _close(row)


def mark_attempt(row: OfficeSession) -> int:
    pending = dict(row.pending_save or {})
    pending["attempts"] = int(pending.get("attempts", 0)) + 1
    row.pending_save = pending
    return pending["attempts"]


def mark_waiting(row: OfficeSession, reason: str) -> bool:
    """Record that the pending save waits on its computer for *reason*; whether that is news to its chats."""
    pending = dict(row.pending_save or {})
    if pending.get("waiting") == reason:
        return False
    pending["waiting"] = reason
    row.pending_save = pending
    return True


def next_try(row: OfficeSession) -> None:
    """Let the next try ask the computer anew: a request joined again only repeats what it got."""
    pending = dict(row.pending_save or {})
    left = int(pending.get("try", 0))
    if pending.get("waiting") == "waiting":
        # The request it leaves may still be open on the computer, for the save job to cancel.
        pending["abandoned"] = [*(pending.get("abandoned") or []), {"key": pending["key"], "try": left}]
    pending["try"] = left + 1
    row.pending_save = pending


def drop_pending(row: OfficeSession) -> tuple[str, str]:
    """Give up the pending save; the (bucket, key) of its blob, to delete. A closing save's row closes."""
    pending = row.pending_save or {}
    blob = (str(pending["bucket"]), str(pending["key"]))
    if pending.get("close"):
        _close(row)
    row.pending_save = None
    return blob


async def close_never_opened(session_factory: Any, *, older_than: timedelta, now: datetime | None = None) -> int:
    """Close rows whose editor never connected (a config fetched, never opened), untouched for *older_than*."""
    cutoff = (now or datetime.now(timezone.utc)) - older_than
    async with session_factory() as db, db.begin():
        result = await db.execute(
            update(OfficeSession)
            .where(
                OfficeSession.status == OPEN,
                OfficeSession.connected_at.is_(None),
                OfficeSession.pending_save.is_(None),
                OfficeSession.updated_at < cutoff,
            )
            .values(status=CLOSED, closed_at=datetime.now(timezone.utc))
        )
        return result.rowcount or 0


def _close(row: OfficeSession) -> None:
    row.status = CLOSED
    row.closed_at = datetime.now(timezone.utc)
