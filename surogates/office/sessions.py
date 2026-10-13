"""Documents open in the document server: one ``office_sessions`` row per open file."""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class OpenDocument:
    id: UUID
    doc_key: str
    path: str
    session_ids: tuple[UUID, ...]
    base_version: str | None
    editors: tuple[str, ...]
    pending_save: dict[str, Any] | None


def _snapshot(row: OfficeSession) -> OpenDocument:
    return OpenDocument(
        id=row.id,
        doc_key=row.doc_key,
        path=row.path,
        session_ids=tuple(UUID(str(value)) for value in row.session_ids),
        base_version=row.base_version,
        editors=tuple(str(value) for value in row.editors),
        pending_save=dict(row.pending_save) if row.pending_save is not None else None,
    )


async def open_or_reuse(session_factory: Any, *, location: str, path: str, session_id: UUID) -> OpenDocument:
    """The open row for *location*, created when there is none; *session_id* joins its chats."""
    async with session_factory() as db, db.begin():
        await db.execute(
            pg_insert(OfficeSession)
            .values(
                id=uuid4(), path=path, location=location, session_ids=[str(session_id)],
                doc_key=uuid4().hex, status=OPEN,
            )
            .on_conflict_do_nothing(index_elements=["location"], index_where=text("status = 'open'"))
        )
        row = await db.scalar(
            select(OfficeSession)
            .where(OfficeSession.location == location, OfficeSession.status == OPEN)
            .with_for_update()
        )
        if row is None:  # closed between the insert and the select: open a fresh one
            row = OfficeSession(
                id=uuid4(), path=path, location=location, session_ids=[str(session_id)],
                doc_key=uuid4().hex, status=OPEN,
            )
            db.add(row)
        elif str(session_id) not in row.session_ids:
            row.session_ids = [*row.session_ids, str(session_id)]
        await db.flush()
        return _snapshot(row)


async def get_open(session_factory: Any, row_id: UUID) -> OpenDocument | None:
    async with session_factory() as db:
        row = await db.scalar(select(OfficeSession).where(OfficeSession.id == row_id, OfficeSession.status == OPEN))
        return _snapshot(row) if row is not None else None


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


async def set_pending(session_factory: Any, row_id: UUID, *, bucket: str, key: str, close: bool) -> str | None:
    """Record a save waiting to land; returns the key of the pending save it replaced."""
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is None:
            return None
        previous = dict(row.pending_save or {})
        row.pending_save = {
            "bucket": bucket,
            "key": key,
            "close": close or bool(previous.get("close")),
            "attempts": 0,
        }
        replaced = previous.get("key")
        return str(replaced) if replaced and replaced != key else None


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


async def close(session_factory: Any, row_id: UUID) -> None:
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is not None and row.status == OPEN:
            row.pending_save = None
            _close(row)


@asynccontextmanager
async def claim_pending(session_factory: Any, *, limit: int = 20) -> AsyncIterator[list[OfficeSession]]:
    """Rows with a save to land, locked so no other worker lands them meanwhile.

    The caller changes the rows (``mark_*``); the changes commit when the block ends.
    """
    async with session_factory() as db, db.begin():
        rows = list(
            (
                await db.scalars(
                    select(OfficeSession)
                    .where(OfficeSession.pending_save.is_not(None))
                    .order_by(OfficeSession.updated_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        yield rows


def mark_landed(row: OfficeSession, version: str | None) -> None:
    close_after = bool((row.pending_save or {}).get("close"))
    row.base_version = version
    row.pending_save = None
    if close_after:
        _close(row)


def mark_closed(row: OfficeSession) -> None:
    row.pending_save = None
    _close(row)


def mark_attempt(row: OfficeSession) -> int:
    pending = dict(row.pending_save or {})
    pending["attempts"] = int(pending.get("attempts", 0)) + 1
    row.pending_save = pending
    return pending["attempts"]


async def close_never_opened(session_factory: Any, *, older_than: timedelta, now: datetime | None = None) -> int:
    """Close rows whose editor never connected (a config fetched, never opened)."""
    cutoff = (now or datetime.now(timezone.utc)) - older_than
    async with session_factory() as db, db.begin():
        result = await db.execute(
            update(OfficeSession)
            .where(
                OfficeSession.status == OPEN,
                OfficeSession.connected_at.is_(None),
                OfficeSession.pending_save.is_(None),
                OfficeSession.created_at < cutoff,
            )
            .values(status=CLOSED, closed_at=datetime.now(timezone.utc))
        )
        return result.rowcount or 0


def _close(row: OfficeSession) -> None:
    row.status = CLOSED
    row.closed_at = datetime.now(timezone.utc)
