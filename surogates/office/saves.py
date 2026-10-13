"""Landing saves from the document editor in the workspace."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import sessions as office_sessions
from surogates.office.chats import live_session
from surogates.office.commands import drop_editors
from surogates.office.runtime_office import close_pages
from surogates.session.events import EventType
from surogates.session.files import session_files
from surogates.tools.workspace_io import RevisionConflict, StorageWorkspaceIO

logger = logging.getLogger(__name__)

# After this many failed tries the chats hear about it; the save keeps retrying.
SAVE_FAILED_AFTER = 10
# Rows one pass lands at most, one transaction each.
CLAIMS_PER_PASS = 20


def conflict_copy_paths(path: str, when: datetime) -> Iterator[str]:
    """Names for a save that found its file changed: beside the original."""
    pure = PurePosixPath(path)
    label = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H.%M")
    stem = f"{pure.stem} (edited {label} UTC)"
    parent = "" if str(pure.parent) == "." else f"{pure.parent}/"
    yield f"{parent}{stem}{pure.suffix}"
    for number in range(2, 100):
        yield f"{parent}{stem} {number}{pure.suffix}"


async def land_pending_saves(*, session_factory: Any, session_store: Any, storage: Any, redis: Any, office: OfficeSettings) -> int:
    """Land up to :data:`CLAIMS_PER_PASS` pending saves; how many landed."""
    landed = 0
    tried: set[UUID] = set()
    for _ in range(CLAIMS_PER_PASS):
        blob: tuple[str, str] | None = None
        async with office_sessions.claim_pending(session_factory) as row:
            # A row back at the front failed earlier in this pass: every older one was tried.
            if row is None or row.id in tried:
                break
            tried.add(row.id)
            try:
                blob = await _land(row, session_factory=session_factory, session_store=session_store,
                                   storage=storage, redis=redis, office=office)
                landed += 1
            except Exception:
                # Commits with the row; its new updated_at moves it behind the others.
                attempts = office_sessions.mark_attempt(row)
                logger.exception("could not land the save of %s (attempt %d)", row.path, attempts)
                if attempts == SAVE_FAILED_AFTER:
                    await _announce(session_store, row.session_ids, EventType.OFFICE_SAVE_FAILED,
                                    {"path": row.path, "attempts": attempts})
        if blob is not None:  # only once the row no longer points at it
            try:
                await storage.delete(*blob)
            except Exception:
                logger.warning("could not delete the landed save %s/%s", *blob, exc_info=True)
    return landed


async def _land(row: OfficeSession, *, session_factory: Any, session_store: Any, storage: Any, redis: Any, office: OfficeSettings) -> tuple[str, str]:
    """Land *row*'s pending save; the (bucket, key) of the save, consumed, to delete once the row commits."""
    pending = dict(row.pending_save or {})
    blob = (pending["bucket"], pending["key"])
    session = await live_session(session_store, row.session_ids)
    if session is None:  # every chat is gone: nothing to save into
        office_sessions.mark_closed(row)
        return blob
    data = await storage.read(*blob)
    async with session_files(session, storage=storage, session_factory=session_factory, redis=redis) as files:
        if not isinstance(files, StorageWorkspaceIO):
            raise RuntimeError(f"{row.path}: saving into a folder on a computer is not supported")
        key = await files.resolve(row.path)
        try:
            await files.write_if(key, data, expected=row.base_version)
        except RevisionConflict:
            # An earlier landing may have written these bytes and stopped before its row
            # change committed. The etag comes first: a write between the two then shows
            # as a conflict at the next save, never as landed.
            version = await files.etag(key)
            try:
                current = await files.read(key)
            except FileNotFoundError:
                current = None
            if current == data:
                office_sessions.mark_landed(row, version)
                return blob
            copy_path = await _write_copy(files, row.path, data)
            await _announce(session_store, row.session_ids, EventType.OFFICE_CONFLICT,
                            {"path": row.path, "conflict_path": copy_path})
            await drop_editors(office, row.doc_key, [str(user) for user in row.editors])
            await close_pages(office, row.doc_key, row.session_ids)
            office_sessions.mark_closed(row)
        else:
            office_sessions.mark_landed(row, await files.etag(key))
    return blob


async def _write_copy(files: StorageWorkspaceIO, path: str, data: bytes) -> str:
    for candidate in conflict_copy_paths(path, datetime.now(timezone.utc)):
        try:
            await files.write_if(await files.resolve(candidate), data, expected=None)
        except RevisionConflict:
            continue
        return candidate
    raise RuntimeError(f"no free name for a copy of {path}")


async def _announce(session_store: Any, session_ids: list[Any], event_type: EventType, data: dict[str, Any]) -> None:
    for session_id in session_ids:
        try:
            await session_store.emit_event(UUID(str(session_id)), event_type, data)
        except Exception:
            logger.warning("could not tell session %s about %s", session_id, event_type.value, exc_info=True)
