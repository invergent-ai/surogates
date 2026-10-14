"""The files a document open in the editor is read from and saved to."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from surogates.db.models import Workstream
from surogates.devices.binding import binding_of, copy_of, device_of
from surogates.devices.operations import REQUEST_PREFIX
from surogates.devices.workspace import MAX_NAMES, DeviceWorkspaceIO
from surogates.sandbox.pool import sandbox_session_key
from surogates.session.files import DeviceAccess, session_files
from surogates.session.store import SessionStore
from surogates.tools.workspace_io import StorageWorkspaceIO
from surogates.workstreams.stream import project_of

# The workspaces the editor opens documents in and saves them to.
DOCUMENT_FILES = (StorageWorkspaceIO, DeviceWorkspaceIO)
# The request ids of the editor's writes to a computer. No route's request id holds a dot, so none is taken for one,
# and the desktop knows them (desktop/src/binding/approvals.ts).
EDITOR_REQUEST = "editor."
# The desktop's words for its user's explicit no to a change (desktop/src/binding/approvals.ts, DENIED.change).
DENIED_ON_COMPUTER = "The user denied this change on this computer"


def editor_request_id(doc_key: str, pending: dict[str, Any]) -> str:
    """The request id of a pending save's write to a computer: its blob, and the try that asks anew."""
    return f"{doc_key}.{PurePosixPath(str(pending['key'])).name}.{int(pending.get('try', 0))}"


def editor_invocation(doc_key: str, pending: dict[str, Any]) -> str:
    """The device invocation of that write, as ``session_files`` names it."""
    return f"{REQUEST_PREFIX}{EDITOR_REQUEST}{editor_request_id(doc_key, pending)}"


def _folded(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


async def on_disk_spelling(files: DeviceWorkspaceIO, path: str) -> str:
    """*path*, relative to the folder, spelled as the computer's folder lists it.

    A folder that ignores case or Unicode form (macOS, Windows) finds ``plan.docx`` for ``Plan.docx``: each name not
    listed as written, but found all the same, takes the one listed spelling, so one file is one document however it
    is asked for. A name the computer does not find stays as written, with the rest of the path: a new file, or a
    folder that tells case apart. So does one in a folder it may not list, or lists only the first MAX_NAMES names
    of, which may leave out the name as written.
    """
    parts = [part for part in path.split("/") if part not in ("", ".")]
    done: list[str] = []
    for index, part in enumerate(parts):
        try:
            names = await files.list_dir(await files.resolve("/".join(done)))
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return "/".join([*done, *parts[index:]])
        if part not in names:
            matches = [name for name in names if _folded(name) == _folded(part)]
            if (len(names) >= MAX_NAMES or len(matches) != 1
                    or await files.stat(await files.resolve("/".join([*done, part]))) is None):
                return "/".join([*done, *parts[index:]])
            part = matches[0]
        done.append(part)
    return "/".join(done)


class FolderNotReady(Exception):
    """The folder is not set up on the computer yet, so the editor has nowhere to save."""


async def document_session(session: Any, *, session_factory: Any) -> Any:
    """The session whose files hold *session*'s documents.

    A project thread on a computer, and a helper under one, work in a copy of the folder; the document is the real
    file, which the project's master works in.  Any other session holds its own.
    """
    config = session.config or {}
    if device_of(config) is None or copy_of(config) is None:
        return session
    project_id = project_of(config.get("workspace_boundary"))
    if project_id is None:
        raise FolderNotReady("This thread's project is gone.")
    return await _master_of(session_factory, UUID(str(project_id)))


async def _master_of(session_factory: Any, project_id: UUID) -> Any:
    async with session_factory() as db:
        project = await db.get(Workstream, project_id)
    if project is None or project.master_session_id is None:
        raise FolderNotReady("This thread's project is gone.")
    return await SessionStore(session_factory).get_session(project.master_session_id)


@asynccontextmanager
async def editor_files(
    session: Any, *, storage: Any, session_factory: Any, redis: Any, request_id: str, data: bytes,
) -> AsyncIterator[Any]:
    """*session*'s files for one write of the editor's.

    On a computer the write is a change request, ``request:editor.<request_id>``, named by *data*'s digest: one the
    caller stops waiting for stays open there, for the same id to join again.  It carries the session owner's access,
    given here once the folder is set up on the computer: the editor acts for that person, and no route is in front
    of it.  A cloud session's files are its own, as before.
    """
    access = change = None
    if device_of(session.config) is not None:
        async with session_factory() as db:
            binding = await binding_of(db, UUID(sandbox_session_key(session)))
        if binding.state != "bound":
            raise FolderNotReady(binding.message or "The folder is not set up on the computer yet.")
        access = DeviceAccess(session_id=session.id, bound=True)
        change = hashlib.sha256(data).hexdigest()
    async with session_files(
        session, storage=storage, session_factory=session_factory, redis=redis,
        request_id=f"{EDITOR_REQUEST}{request_id}", change=change, access=access,
    ) as files:
        yield files
