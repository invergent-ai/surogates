"""Landing saves from the document editor in the workspace."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession, Workstream
from surogates.devices.binding import device_of
from surogates.devices.operations import DeviceOperations, TooManyRequests
from surogates.devices.presence import DevicePresence
from surogates.devices.workspace import said
from surogates.office import sessions as office_sessions
from surogates.office.chats import live_session
from surogates.office.commands import drop_editors
from surogates.office.files import (
    DENIED_ON_COMPUTER, DOCUMENT_FILES, document_session, editor_files, editor_invocation, editor_request_id,
    document_files,
)
from surogates.office.runtime_office import close_pages
from surogates.session.events import EventType
from surogates.session.files import ComputerAway
from surogates.session.store import SessionNotFoundError
from surogates.tools.workspace_io import RevisionConflict
from surogates.workstreams import is_project_thread
from surogates.workstreams.stream import project_of
from surogates.workstreams.undo import BUSY, Refused, land_editor_save

logger = logging.getLogger(__name__)

# After this many failed tries the chats hear about it; the save keeps retrying.
SAVE_FAILED_AFTER = 10
# Rows one pass lands at most, one transaction each.
CLAIMS_PER_PASS = 20
# How long one pass waits on a computer for a save: one still waiting is the next pass's to join again.
# ponytail: a waiting row holds the serial pass, and its own row lock, for up to this on every pass, so many saves
# waiting on computers delay the others; claim rows released, or run the waits concurrently, to lift it.
COMPUTER_WAIT_S = 5.0


class SaveWaiting(Exception):
    """The save waits on its computer, to come online or for its user to allow it: no failed try."""

    def __init__(self, reason: str, computer: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.computer = computer


class SaveDeclined(Exception):
    """The computer's user explicitly denied the save: only that, never a prompt left unanswered."""

    def __init__(self, computer: str) -> None:
        super().__init__(computer)
        self.computer = computer


def conflict_copy_paths(path: str, when: datetime) -> Iterator[str]:
    """Names for a save that found its file changed: beside the original."""
    pure = PurePosixPath(path)
    label = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H.%M")
    stem = f"{pure.stem} (edited {label} UTC)"
    parent = "" if str(pure.parent) == "." else f"{pure.parent}/"
    yield f"{parent}{stem}{pure.suffix}"
    for number in range(2, 100):
        yield f"{parent}{stem} {number}{pure.suffix}"


async def land_pending_saves(*, session_factory: Any, session_store: Any, storage: Any, redis: Any, office: OfficeSettings,
                             settings: Any = None) -> int:
    """Land up to :data:`CLAIMS_PER_PASS` pending saves; how many landed.

    *settings* are the runtime's settings, whose history and saga sections land a project's saves.
    """
    landed = 0
    tried: set[UUID] = set()
    for _ in range(CLAIMS_PER_PASS):
        blob: tuple[str, str] | None = None
        async with office_sessions.claim_pending(session_factory) as row:
            # A row back at the front failed earlier in this pass: every older one was tried.
            if row is None or row.id in tried:
                break
            tried.add(row.id)
            waited = bool((row.pending_save or {}).get("waiting"))
            try:
                blob, in_place = await _land(row, session_factory=session_factory, session_store=session_store,
                                             storage=storage, redis=redis, office=office, settings=settings)
                landed += 1
                # A conflict copy said so itself, and gone chats hear nothing.
                if waited and in_place:
                    await _announce(session_store, row.session_ids, EventType.OFFICE_SAVED, {"path": row.path})
            except SaveWaiting as waiting:
                # Not a failed try. Its new updated_at moves the row behind the others.
                row.updated_at = datetime.now(timezone.utc)
                if waiting.reason == "offline":
                    office_sessions.next_try(row)  # the change it left open was cancelled
                if office_sessions.mark_waiting(row, waiting.reason):
                    await _announce(session_store, row.session_ids, EventType.OFFICE_SAVE_WAITING,
                                    {"path": row.path, "computer": waiting.computer, "reason": waiting.reason})
                logger.info("the save of %s waits on %s (%s)", row.path, waiting.computer, waiting.reason)
            except SaveDeclined as declined:
                # The edits stay in the editor: its next save asks again.
                blob = office_sessions.drop_pending(row)
                logger.info("%s declined the save of %s", declined.computer, row.path)
                await _announce(session_store, row.session_ids, EventType.OFFICE_SAVE_FAILED,
                                {"path": row.path, "reason": "declined", "computer": declined.computer})
            except Exception as exc:
                if isinstance(exc, Refused) and str(exc) == BUSY:
                    # The project is busy: not a failed try. Its new updated_at moves the row behind the others.
                    row.updated_at = datetime.now(timezone.utc)
                    logger.info("the project of %s is busy: its save waits", row.path)
                    continue
                # A computer's request joined again would only repeat this failure: the next try asks anew.
                office_sessions.next_try(row)
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


async def _land(row: OfficeSession, *, session_factory: Any, session_store: Any, storage: Any, redis: Any, office: OfficeSettings,
                settings: Any = None) -> tuple[tuple[str, str], bool]:
    """Land *row*'s pending save; the (bucket, key) of the save, consumed, to delete once the row commits, and whether
    it landed over the file (not as a conflict copy, nor dropped with every chat gone).

    *settings* are the runtime's settings, whose history and saga sections land a project's saves, and whose storage
    section names a case's workspace.
    """
    pending = dict(row.pending_save or {})
    blob = (pending["bucket"], pending["key"])
    if row.case_id is not None:
        if settings is None:
            raise RuntimeError("landing a case's save needs the runtime's settings")
        # A case's document saves into the case's workspace: no chat to save into, no computer to wait on.
        files = document_files(row, storage=storage, settings=settings)
        copy_path = await _write(row, files, await storage.read(*blob), project=None, session_factory=session_factory,
                                 session_store=session_store, storage=storage, redis=redis, settings=settings)
    else:
        session = await live_session(session_store, row.session_ids)
        if session is None:  # every chat is gone: nothing to save into
            office_sessions.mark_closed(row)
            return blob, False
        copy_path = await _land_in_chat(row, session, blob, session_factory=session_factory, session_store=session_store,
                                        storage=storage, redis=redis, settings=settings)
    if copy_path is not None:
        await _announce(session_store, row.session_ids, EventType.OFFICE_CONFLICT,
                        {"path": row.path, "conflict_path": copy_path})
        await drop_editors(office, row.doc_key, [str(user) for user in row.editors])
        await close_pages(office, row.doc_key, row.session_ids)
        office_sessions.mark_closed(row)
    return blob, copy_path is None


async def _land_in_chat(row: OfficeSession, session: Any, blob: tuple[str, str], *, session_factory: Any,
                        session_store: Any, storage: Any, redis: Any, settings: Any) -> str | None:
    """Land *row*'s pending save, *blob*, in the files of *session*, a chat it is open in; the conflict copy's path,
    or None."""
    pending = dict(row.pending_save or {})
    target = await document_session(session, session_factory=session_factory)
    computer = _computer_of(target)
    if computer is not None:
        await _cancel_abandoned(row, target, session_factory=session_factory, redis=redis)
        device_id = device_of(target.config)
        # Asking an offline computer would only claim a request to cancel it, after reading the save for nothing.
        if device_id not in await DevicePresence(redis).online([device_id]):
            raise SaveWaiting("offline", computer)
    data = await storage.read(*blob)
    deadline = None
    try:
        async with editor_files(target, storage=storage, session_factory=session_factory, redis=redis,
                                request_id=editor_request_id(row.doc_key, pending), data=data) as files:
            if not isinstance(files, DOCUMENT_FILES):
                raise RuntimeError(f"{row.path}: this workspace cannot take the editor's saves")
            # On a computer the project's own landing records it, as yours: the save is the folder's, through its
            # master.
            project = None if computer is not None else await _project_of(session_factory, session)
            # Only the computer's operations run under its wait: a pass joining this request again asks for the same.
            async with asyncio.timeout(COMPUTER_WAIT_S if computer is not None else None) as deadline:
                copy_path = await _write(row, files, data, project=project, session_factory=session_factory,
                                         session_store=session_store, storage=storage, redis=redis, settings=settings)
    except ComputerAway as away:
        if away.revoked:
            raise
        raise SaveWaiting("offline", away.name) from None
    except TooManyRequests:
        if computer is None:
            raise
        raise SaveWaiting("waiting", computer) from None
    except TimeoutError:
        # Only this pass's own wait running out: a timeout the computer answered (a network folder's) is a failure.
        if computer is None or deadline is None or not deadline.expired():
            raise
        raise SaveWaiting("waiting", computer) from None
    except PermissionError as exc:
        # Only the user's explicit no declines it. Any other refusal (nobody answered, it could not ask, the file is
        # locked) is retried: a closing save dropped is lost.
        if computer is None or DENIED_ON_COMPUTER not in said(exc):
            raise
        raise SaveDeclined(computer) from None
    return copy_path


async def _cancel_abandoned(row: OfficeSession, target: Any, *, session_factory: Any, redis: Any) -> None:
    """Cancel the writes that saves this one replaced, or tries it left, may still have open on the computer."""
    pending = dict(row.pending_save or {})
    abandoned = pending.pop("abandoned", None) or []
    if not abandoned:
        return
    operations = DeviceOperations(session_factory, redis)
    for item in abandoned:
        try:
            await operations.cancel_invocation(target.id, editor_invocation(row.doc_key, item))
        except Exception:  # one that still lands shows as a conflict copy
            logger.warning("could not cancel an abandoned save of %s on its computer", row.path, exc_info=True)
    row.pending_save = pending


def _computer_of(session: Any) -> str | None:
    """The name of the computer *session*'s files are on, or None in the cloud."""
    if device_of(session.config) is None:
        return None
    return str((session.config.get("execution") or {}).get("device_name") or "your computer")


async def _write(row: OfficeSession, files: Any, data: bytes, *, project: Workstream | None, session_factory: Any,
                 session_store: Any, storage: Any, redis: Any, settings: Any) -> str | None:
    """Write the save over the version the editor opened, or, when the file changed since, beside it as a copy;
    the copy's path, or None.  A save to *project*'s real files lands as the project's own landing records it."""
    pending = dict(row.pending_save or {})
    key = await files.resolve(row.path)
    try:
        if project is None:
            await files.write_if(key, data, expected=row.base_version)
        else:
            if settings is None:
                raise RuntimeError("landing a project's save needs the runtime's settings")
            state = SimpleNamespace(session_factory=session_factory, session_store=session_store, storage=storage,
                                    settings=settings, redis=redis)
            await land_editor_save(
                state, project, path=row.path,
                author=await editor_author(session_store, project, list(pending.get("authors") or [])),
                write=lambda: files.write_if(key, data, expected=row.base_version),
            )
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
            return None
        # Named by the save's own time, not the clock: a pass joining this request again asks for the same copy.
        lastsave = pending.get("lastsave")
        return await _write_copy(files, row.path, data,
                                 datetime.fromisoformat(lastsave) if lastsave else datetime.now(timezone.utc))
    office_sessions.mark_landed(row, await files.etag(key))
    return None


async def _project_of(session_factory: Any, session: Any) -> Workstream | None:
    """The project whose real files the session works in, or None for a plain chat."""
    project_id = project_of(session.config.get("workspace_boundary"))
    if project_id is None:
        return None
    async with session_factory() as db:
        return await db.get(Workstream, UUID(project_id))


async def editor_author(session_store: Any, project: Any, authors: list[str]) -> dict[str, str]:
    """Who the history names for an editor save: the project's thread whose agent alone edited it; you whenever a
    person edited too, several chats' agents did, or the agent's chat is not one of the project's threads."""
    you = {"name": str(project.user_id), "email": f"user:{project.user_id}@surogate"}
    agents = {author for author in authors if author.startswith("agent:")}
    if len(agents) != 1 or len(agents) != len(set(authors)):
        return you
    try:
        thread = await session_store.get_session(UUID(next(iter(agents)).rsplit(":", 1)[-1]))
    except (ValueError, SessionNotFoundError):
        return you
    # Only this project's own thread is named: an id from anywhere else, or its master's, is not trusted to be one.
    if not is_project_thread(thread.config) or str((thread.config or {}).get("workstream_id")) != str(project.id):
        return you
    return {"name": thread.title or "Thread", "email": f"thread:{thread.id}@surogate"}


async def _write_copy(files: Any, path: str, data: bytes, when: datetime) -> str:
    for candidate in conflict_copy_paths(path, when):
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
