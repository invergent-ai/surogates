"""Opening a document in the agent's own editor session, in runtime-office."""

from __future__ import annotations

import posixpath
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from surogates.config import OfficeSettings
from surogates.devices.binding import device_of
from surogates.devices.workspace import DeviceOperationError, DeviceWorkspaceIO, said
from surogates.office import sessions as office_sessions
from surogates.office.editor_config import build_editor_config
from surogates.office.files import (
    DENIED_ON_COMPUTER,
    DOCUMENT_FILES,
    FolderNotReady,
    document_session,
    editor_files,
    on_disk_spelling,
)
from surogates.office.kinds import office_kind
from surogates.office.runtime_office import RuntimeOffice
from surogates.session.attachment_ingest import workspace_root_id
from surogates.session.files import ComputerAway, session_files
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError, refuse_nul
from surogates.tools.workspace_io import RevisionConflict, StorageWorkspaceIO
from surogates.workstreams import is_project_thread

TEMPLATES = Path(__file__).with_name("templates")
#: The documents the agent edits live. A new one starts from the document server's own blank of its type.
AGENT_KINDS = ("docx", "xlsx", "pptx", "pdf")
# How a path names its user's home folder.
_HOMES = ("~", "$HOME", "${HOME}")


def blank(file_type: str) -> Path:
    return TEMPLATES / f"blank.{file_type}"


class AgentDocumentError(Exception):
    """Why the agent cannot open the document: said to the model as the tool's error."""


@dataclass(frozen=True, slots=True)
class AgentDocument:
    page_id: str
    document: office_sessions.OpenDocument
    path: str
    user_id: str
    created: bool


def agent_user_id(agent_id: str, session_id: UUID | str) -> str:
    """The agent's editor user in one chat: a thread's edits are told apart from another's."""
    return f"agent:{agent_id}:{session_id}"


def relative_path(path: str, session: Any) -> str:
    """*path* as the model may write it (``/workspace/x``, ``__WORKSPACE__/x``, ``~/x``: the sandbox's HOME is the
    workspace), relative to the workspace and normalised; ``.`` is the workspace itself. A ``..`` out of it is kept,
    for the workspace to refuse.  On a computer ``~`` and ``$HOME`` are its user's home, not the folder: they are kept
    as given, and ``within_folder`` refuses them."""
    root = str(session.config.get("workspace_path") or "/workspace").rstrip("/")
    homes = () if device_of(session.config) is not None else _HOMES
    for prefix in (*homes, "__WORKSPACE__", root):
        if path == prefix or path.startswith(prefix + "/"):
            path = path[len(prefix):].lstrip("/")
            break
    return posixpath.normpath(path or ".")


def within_folder(path: str) -> bool:
    """Whether *path*, as ``relative_path`` gives it, names a file inside the workspace: not an absolute path outside
    it, a home folder's, or a way out.  A file whose name only begins like one (``~draft.docx``) is inside."""
    if path.startswith("/") or path == ".." or path.startswith("../"):
        return False
    return not any(path == home or path.startswith(home + "/") for home in _HOMES)


def location_of(session: Any, path: str) -> str:
    """Where *path*, relative to the workspace, is: one name however the file is reached.

    A cloud session names it as the workspace layer does (``StorageWorkspaceIO.location``), literally; a session on
    a computer by its path there, normalised as the pane names it (not as the computer resolves it) and in one
    Unicode form, since a computer's folder may store either.  A project
    thread's copy sits at the folder's own path, so it names the real file, as its master does: the editor only ever
    opens that one.
    """
    device_id = device_of(session.config)
    if device_id is not None:
        folder = str(session.config["workspace_path"]).rstrip("/")
        return f"device:{device_id}:{unicodedata.normalize('NFC', posixpath.join(folder, posixpath.normpath(path)))}"
    prefix = boundary_workspace_prefix(session.config, session, workspace_root_id(session))
    return StorageWorkspaceIO(None, bucket=session.config["storage_bucket"], prefix=prefix).location(path)


async def open_for_agent(
    *, session: Any, agent_id: str, agent_name: str, path: str, create: bool, office: OfficeSettings,
    runtime: RuntimeOffice, storage: Any, session_factory: Any, redis: Any,
) -> AgentDocument:
    """The agent's editor page on the document at *path*, made blank first when *create* and it does not exist.

    A project thread on a computer opens the real file, through its project's master: its own copy is not the
    document."""
    kind = office_kind(path)
    if kind is None or kind.file_type not in AGENT_KINDS:
        raise AgentDocumentError(
            "office_read and office_edit work on Word documents, spreadsheets, slide decks and PDFs (.docx, .xlsx, .pptx, .pdf)."
        )
    try:
        refuse_nul(path)  # before the open rows are looked up by it
    except ValueError as exc:
        raise AgentDocumentError(str(exc)) from None
    created = False
    try:
        target = await document_session(session, session_factory=session_factory)
        location = location_of(target, path)
        # The editor has it open, so it exists: asking the computer on every call would keep its folder busy.
        if await office_sessions.open_at(session_factory, location) is None:
            async with session_files(target, storage=storage, session_factory=session_factory, redis=redis) as files:
                if not isinstance(files, DOCUMENT_FILES):
                    raise AgentDocumentError("This workspace cannot be edited live yet.")
                try:
                    if isinstance(files, DeviceWorkspaceIO):
                        path = await on_disk_spelling(files, path)
                    key = await files.resolve(path)
                except (WorkspaceSandboxError, ValueError) as exc:  # a traversal, or a NUL in the path
                    raise AgentDocumentError(str(exc)) from None
                location = location_of(target, path)
                # Asked for in another spelling of a document the editor has open: that one.
                missing = await office_sessions.open_at(session_factory, location) is None and await files.etag(key) is None
            if missing:
                if not create:
                    raise AgentDocumentError(
                        f"No document at {path} in the project's files: one this thread made this turn reaches them when the turn ends."
                        if is_project_thread(session.config) else f"No document at {path}."
                    )
                created = await _make_blank(target, path, kind.file_type, storage=storage, session_factory=session_factory, redis=redis)
    except FolderNotReady as exc:
        raise AgentDocumentError(str(exc)) from None
    except ComputerAway as exc:
        raise AgentDocumentError(f"{exc}: the document opens once it is back.") from None
    except (DeviceOperationError, OSError) as exc:
        if isinstance(exc, PermissionError) and DENIED_ON_COMPUTER in said(exc):
            raise AgentDocumentError(f"{path} was not made: the computer's user declined it.") from None
        raise AgentDocumentError(said(exc)) from None
    user_id = agent_user_id(agent_id, session.id)
    try:
        document = await office_sessions.open_waiting(
            session_factory, location=location, path=path, session_id=session.id, user=(user_id, agent_name),
        )
    except office_sessions.DocumentClosing:
        raise AgentDocumentError("The document is still being saved; try again in a moment.") from None
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=path, kind=kind,
        user_id=user_id, user_name=agent_name, access="edit", theme="light", plugins=False,
    )
    page = await runtime.open(doc_key=document.doc_key, session_id=str(session.id), config=config,
                              document_server_url=office.internal_url)
    return AgentDocument(page_id=page, document=document, path=path, user_id=user_id, created=created)


async def _make_blank(target: Any, path: str, file_type: str, *, storage: Any, session_factory: Any, redis: Any) -> bool:
    """Make *path* a blank document; whether this call made it. On a computer, through the editor's own request,
    which its app asks about once in a folder that asks every time."""
    blank_bytes = blank(file_type).read_bytes()
    async with editor_files(target, storage=storage, session_factory=session_factory, redis=redis,
                            request_id=f"new.{uuid4().hex}", data=blank_bytes) as files:
        try:
            await files.write_if(await files.resolve(path), blank_bytes, expected=None)
        except RevisionConflict:
            return False  # someone made it meanwhile: open theirs
    return True
