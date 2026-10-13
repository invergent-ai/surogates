"""Opening a document in the agent's own editor session, in runtime-office."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from surogates.config import OfficeSettings
from surogates.devices.binding import device_of
from surogates.office import sessions as office_sessions
from surogates.office.editor_config import build_editor_config
from surogates.office.kinds import office_kind
from surogates.office.runtime_office import RuntimeOffice
from surogates.session.attachment_ingest import workspace_root_id
from surogates.session.files import session_files
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError
from surogates.tools.workspace_io import RevisionConflict, StorageWorkspaceIO
from surogates.workstreams import is_project_thread

BLANK_DOCX = Path(__file__).with_name("templates") / "blank.docx"


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
    """*path* as the model may write it (``/workspace/x``, ``__WORKSPACE__/x``), relative to the workspace."""
    root = str(session.config.get("workspace_path") or "/workspace").rstrip("/") + "/"
    for prefix in ("__WORKSPACE__/", root):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def location_of(session: Any, path: str) -> str:
    """Where *path* is in a cloud session's workspace, as the workspace layer names it (``StorageWorkspaceIO.location``)."""
    prefix = boundary_workspace_prefix(session.config, session, workspace_root_id(session))
    return StorageWorkspaceIO(None, bucket=session.config["storage_bucket"], prefix=prefix).location(path)


async def open_for_agent(
    *, session: Any, agent_id: str, agent_name: str, path: str, create: bool, office: OfficeSettings,
    runtime: RuntimeOffice, storage: Any, session_factory: Any, redis: Any,
) -> AgentDocument:
    """The agent's editor page on the document at *path*, made blank first when *create* and it does not exist."""
    kind = office_kind(path)
    if kind is None or kind.file_type != "docx":
        raise AgentDocumentError("office_read and office_edit work on Word documents (.docx) for now.")
    if device_of(session.config) is not None:
        raise AgentDocumentError("Documents in a folder on a computer cannot be edited live yet.")
    created = False
    async with session_files(session, storage=storage, session_factory=session_factory, redis=redis) as files:
        if not isinstance(files, StorageWorkspaceIO):
            raise AgentDocumentError("This workspace cannot be edited live yet.")
        try:
            key = await files.resolve(path)
        except (WorkspaceSandboxError, ValueError) as exc:  # a traversal, or a NUL in the path
            raise AgentDocumentError(str(exc)) from None
        if await files.etag(key) is None:
            if not create:
                raise AgentDocumentError(
                    f"No document at {path} in the project's files: one this thread made this turn reaches them when the turn ends."
                    if is_project_thread(session.config) else f"No document at {path}."
                )
            try:
                await files.write_if(key, BLANK_DOCX.read_bytes(), expected=None)
                created = True
            except RevisionConflict:
                pass  # someone made it meanwhile: open theirs
        location = files.location(key)
    user_id = agent_user_id(agent_id, session.id)
    try:
        document = await office_sessions.open_waiting(
            session_factory, location=location, path=path, session_id=session.id, user=(user_id, agent_name),
        )
    except office_sessions.DocumentClosing:
        raise AgentDocumentError("The document is still being saved; try again in a moment.") from None
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=path, kind=kind,
        user_id=user_id, user_name=agent_name, edit=True, theme="light", plugins=False,
    )
    page = await runtime.open(doc_key=document.doc_key, session_id=str(session.id), config=config,
                              document_server_url=office.internal_url)
    return AgentDocument(page_id=page, document=document, path=path, user_id=user_id, created=created)

