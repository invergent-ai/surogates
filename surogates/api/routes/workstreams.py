"""Projects: the signed-in user's coordinator chats, kept as ``workstreams``.

Each project is a row and its master web session, the coordinator.  Every
route is scoped to the signed-in user and needs the agent's "multi session"
capability: with it off, every web session but the canonical one is hidden.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import unicodedata
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints, model_validator
from sse_starlette.sse import EventSourceResponse
from starlette.types import Receive, Scope, Send

from surogates.api.routes.sessions import DeviceExecution, _require_local_device, archive_session_tree
from surogates.api.routes.workspace import _should_skip_dir
from surogates.api.session_guards import require_device_access
from surogates.db.models import Workstream
from surogates.devices.operations import DeviceOperations
from surogates.devices.presence import DevicePresence
from surogates.harness.loop_artifacts import _coerce_modified_to_datetime
from surogates.harness.turn_summarizer import is_platform_path
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep, rate_limit_dep
from surogates.sandbox.history import HistoryError
from surogates.session.models import Session
from surogates.session.provisioning import create_agent_session
from surogates.session.store import SessionNotFoundError
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.workstreams import master_config
from surogates.workstreams import stream as project_stream
from surogates.workstreams.bucket import NOT_KEPT, BucketHistory, Busy, NotKept, Staged, landable, said
from surogates.workstreams.derive import SHELL_LIMITS, aware, derive_thread, place_of, units, utc
from surogates.workstreams.history import HISTORY_OFF, deleted_files, over_history_cap, row_id, version_of, versions
from surogates.workstreams.store import WorkstreamStore
from surogates.workstreams.threads import begin_thread, make_thread, start_thread, stop_thread
from surogates.workstreams.undo import NO_CHANGE, UNLANDED, Refused, restore, undo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workstreams")

AgentRuntime = Annotated[AgentRuntimeContext, Depends(agent_runtime_context_dep)]
Tenant = Annotated[TenantContext, Depends(get_current_tenant)]


def _text(max_length: int, min_length: int = 1):
    def fits(value: str) -> str:
        # Python's strip, as the chat title's: pydantic's keeps U+001C-U+001F,
        # so the name, the title and the prompt would disagree.
        value = value.strip()
        if len(value) < min_length:
            raise ValueError("must not be blank")
        # Counted in UTF-16 units, as the desktop shell counts them: an emoji is two.
        if len(value.encode("utf-16-le")) // 2 > max_length:
            raise ValueError(f"must be at most {max_length} characters")
        return value

    # Postgres text and jsonb refuse NUL, which would fail the write with a 500.
    return Annotated[str, StringConstraints(pattern=r"^[^\x00]*$"), AfterValidator(fits)]


def _whole(value: str) -> str:
    if units(value) > SHELL_LIMITS["ref"]:
        raise ValueError(f"must be at most {SHELL_LIMITS['ref']} characters")
    return value


#: A file's path as History takes it: as it is, never trimmed, with no NUL
#: (jsonb refuses one), and no longer than the shell takes a file's ref.
FilePath = Annotated[str, StringConstraints(pattern=r"^[^\x00]+$"), AfterValidator(_whole)]


def _blank_is_none(max_length: int):
    # A field the user emptied clears the value.
    return Annotated[_text(max_length, min_length=0), AfterValidator(lambda value: value or None)]


# The name is the master chat's title, whose cap is 256.
Name = _text(256)
Goal = _blank_is_none(2000)
Instructions = _text(16_000, min_length=0)
Icon = _blank_is_none(64)
# None: the agent's own tier.
Tier = Literal["basic", "pro"] | None
# What a thread the user resolves while it works is stopped with.
_RESOLVED_BY_USER = "resolved by the user"
# How long a card's start holds its claim at most: a start takes about a second.
_CARD_CLAIM_SECONDS = 60
# Releases a card's claim only while it is still the releasing start's.
_RELEASE_CLAIM = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"


class ProjectCreate(BaseModel):
    name: Name
    goal: Goal | None = None
    instructions: Instructions = ""


class ProjectChange(BaseModel):
    """The fields a change names; a field it leaves out keeps its value."""

    name: Name | None = None
    icon: Icon | None = None
    goal: Goal | None = None
    instructions: Instructions | None = None
    coordinator_tier: Tier = None
    thread_tier: Tier = None

    @model_validator(mode="after")
    def _keep_required(self) -> ProjectChange:
        for field in ("name", "instructions"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be cleared")
        return self


class ProposedThreadStart(BaseModel):
    """A card of a proposal: the thread is read from its ``thread.proposed`` event."""

    proposal_id: UUID
    key: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,3}$")]
    #: A folder its user confirmed on their computer: the thread works there.
    execution: DeviceExecution | None = None


class ProjectSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    icon: str | None
    created_at: datetime
    updated_at: datetime
    # Threads waiting on the user, plus one for a question or approval open in the master.
    waiting: int = 0
    working: int = 0


class ProjectOut(ProjectSummaryOut):
    goal: str | None
    instructions: str
    master_session_id: UUID
    coordinator_tier: str | None
    thread_tier: str | None


def _owner(tenant: TenantContext, ctx: AgentRuntimeContext) -> dict:
    """The owner columns every project query is scoped to."""
    if tenant.user_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Projects belong to a user account.")
    if str(tenant.org_id) != ctx.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This agent belongs to another organization.")
    if not ctx.multi_session:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This agent keeps a single conversation, so it has no projects.",
        )
    return {"org_id": tenant.org_id, "agent_id": ctx.agent_id, "user_id": tenant.user_id}


def _store(request: Request) -> WorkstreamStore:
    return WorkstreamStore(request.app.state.session_factory)


async def _project(request: Request, workstream_id: UUID, tenant: TenantContext, ctx: AgentRuntimeContext) -> Workstream:
    """The signed-in user's live project *workstream_id*; 404 otherwise."""
    project = await _store(request).get(workstream_id, **_owner(tenant, ctx))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project.")
    return project


async def _described(request: Request, projects: list[Workstream]) -> list[ProjectOut]:
    """*projects* as the shell lists them, the latest active first: each with
    its counts, and its latest activity, the row's, its master's or a
    thread's, whichever is newest."""
    if not projects:
        return []
    store = _store(request)
    threads = await store.thread_counts([project.id for project in projects])
    masters = await store.masters([project.master_session_id for project in projects])
    described = []
    for project in projects:
        if project.master_session_id not in masters:
            # Deleted since the projects were read: its project went with it.
            continue
        seen, asking = masters[project.master_session_id]
        latest, waiting, working = threads.get(project.id, (None, 0, 0))
        described.append(ProjectOut.model_validate(project).model_copy(update={
            "waiting": waiting + asking,
            "working": working,
            "updated_at": max(aware(moment) for moment in (project.updated_at, seen, latest) if moment is not None),
        }))
    return sorted(described, key=lambda project: project.updated_at, reverse=True)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate, request: Request, ctx: AgentRuntime, tenant: Tenant,
    _rate: None = Depends(rate_limit_dep),
):
    owner = _owner(tenant, ctx)
    workstream_id = uuid4()
    sessions = request.app.state.session_store
    master = await create_agent_session(
        store=sessions,
        storage=request.app.state.storage,
        settings=request.app.state.settings,
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        agent_id=ctx.agent_id,
        channel="web",
        config=master_config(workstream_id, name=body.name, goal=body.goal, instructions=body.instructions),
    )
    try:
        await sessions.update_session_title(master.id, body.name)
        project = await _store(request).create(
            id=workstream_id, master_session_id=master.id,
            name=body.name, goal=body.goal, instructions=body.instructions, **owner,
        )
    except Exception:
        # A master with no project could be neither listed nor archived.
        await sessions.update_session_status(master.id, "archived")
        raise
    [described] = await _described(request, [project])
    return described


@router.get("", response_model=list[ProjectSummaryOut])
async def list_projects(request: Request, ctx: AgentRuntime, tenant: Tenant):
    # The latest active, as many as the shell takes.
    return (await _described(request, await _store(request).list(**_owner(tenant, ctx))))[: SHELL_LIMITS["rows"]]


@router.get("/{workstream_id}", response_model=ProjectOut)
async def get_project(workstream_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant):
    [described] = await _described(request, [await _project(request, workstream_id, tenant, ctx)])
    return described


@router.patch("/{workstream_id}", response_model=ProjectOut)
async def change_project(
    workstream_id: UUID, body: ProjectChange, request: Request, ctx: AgentRuntime, tenant: Tenant,
):
    # The master's next wake reads the change; a running turn keeps its prompt.
    project = await _store(request).change(
        workstream_id, body.model_dump(exclude_unset=True), **_owner(tenant, ctx),
    )
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project.")
    [described] = await _described(request, [project])
    return described


@router.delete("/{workstream_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_project(
    workstream_id: UUID, request: Request, background_tasks: BackgroundTasks,
    ctx: AgentRuntime, tenant: Tenant,
) -> Response:
    """Archive the project with its master's tree, as a deleted chat's goes.

    The project's files are a boundary workspace, so they are kept, and so is
    its memory.
    """
    project = await _project(request, workstream_id, tenant, ctx)
    master = await request.app.state.session_store.get_session(project.master_session_id)
    await archive_session_tree(request, master, background_tasks)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _say_online(request: Request, places: list[dict[str, Any]]) -> None:
    """Say of each computer *places* name whether it is connected to the agent now."""
    devices = {UUID(place["device_id"]) for place in places if place["kind"] == "device"}
    redis = getattr(request.app.state, "redis", None)
    if not devices or redis is None:
        return
    try:
        online = await DevicePresence(redis).online(list(devices))
    except Exception:
        # A nicety: Redis down leaves every computer offline, and the rows still answer.
        logger.warning("Could not tell which computers are online", exc_info=True)
        return
    for place in places:
        if place["kind"] == "device":
            place["online"] = UUID(place["device_id"]) in online


@router.get("/{workstream_id}/threads")
async def list_threads(
    workstream_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant, thread_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """The project's threads as the shell's ``ThreadRow``, in snake_case,
    newest first and as many as the shell takes; or only *thread_id*'s, none
    when it is not one of the project's live threads."""
    project = await _project(request, workstream_id, tenant, ctx)
    now = datetime.now(timezone.utc)
    found = await _store(request).thread_facts(project.id, thread_id=thread_id)
    rows = [derive_thread(facts, now=now) for facts in found[: SHELL_LIMITS["rows"]]]
    await _say_online(request, [row["place"] for row in rows])
    return rows


@router.get("/{workstream_id}/library")
async def project_library(
    workstream_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> list[dict[str, Any]]:
    """The project's files, newest first and as many as the shell takes:
    the one workspace its master and threads share.  A file a cloud
    thread's turn summary named is that thread's, the last one to name it;
    every other file the user added.  The platform's own files are left
    out, and so is every folder the file panel skips (dependencies, builds,
    checkouts): an install's thousands of files would push the user's out.
    A thread on the user's computer made its files there: they are listed
    from its summaries, with that computer as their place."""
    project = await _project(request, workstream_id, tenant, ctx)
    master = await request.app.state.session_store.get_session(project.master_session_id)
    prefix = boundary_workspace_prefix(master.config, master, master.id)
    named, listed = await asyncio.gather(
        _store(request).produced(project.id),
        request.app.state.storage.list_entries(master.config["storage_bucket"], prefix=prefix),
    )
    produced = {path: thread_id for thread_id, path, execution, _ in named if execution is None}
    on_computers = {(thread_id, path): (execution, at) for thread_id, path, execution, at in named if execution is not None}
    entries: list[tuple[datetime, dict[str, Any]]] = []
    for found in listed:
        path = found["key"][len(prefix):]
        if (
            not path
            or path.endswith("/")
            or is_platform_path(path)
            or any(_should_skip_dir(folder) for folder in path.split("/")[:-1])
            or units(path) > SHELL_LIMITS["ref"]
        ):
            continue
        modified = _coerce_modified_to_datetime(found.get("modified"))
        thread_id = produced.get(path)
        entries.append((modified or datetime.min.replace(tzinfo=timezone.utc), {
            "path": path,
            "origin": "added" if thread_id is None else "produced",
            "thread_id": None if thread_id is None else str(thread_id),
            "size": found.get("size"),
            "updated_at": utc(modified),
            "place": {"kind": "cloud"},
        }))
    for (thread_id, path), (execution, at) in on_computers.items():
        if is_platform_path(path, on_folder=True) or units(path) > SHELL_LIMITS["ref"]:
            continue
        entries.append((aware(at), {
            "path": path, "origin": "produced", "thread_id": str(thread_id),
            "size": None, "updated_at": utc(at), "place": place_of(execution),
        }))
    # By the moment, not its text: "…:56Z" would sort after "…:56.5Z".
    entries.sort(key=lambda entry: entry[0], reverse=True)
    shown = [entry for _, entry in entries[: SHELL_LIMITS["library"]]]
    await _say_online(request, [entry["place"] for entry in shown])
    return shown


def _unread(exc: HistoryError | OSError) -> HTTPException:
    """A history the api could not read, in words: past a bound it keeps, said as it is; another request's
    just now; or out of reach, with git's own words, or its disk's, left to the log."""
    if (words := said(exc)) is not None:
        return HTTPException(status.HTTP_409_CONFLICT, words)
    if isinstance(exc, Busy):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc), headers={"Retry-After": "5"})
    logger.warning("Could not read a project's history", exc_info=exc)
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE, "The project's history could not be read just now. Try again in a moment.",
    )


@router.get("/{workstream_id}/history")
async def file_history(
    workstream_id: UUID, path: FilePath, request: Request, ctx: AgentRuntime, tenant: Tenant, device_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """A file's History, its newest versions first: who changed it, when and
    how, read from the project's records.  *path* is a name among those
    records and nothing more: it reaches no storage.  Whether each version
    is still kept is asked of the api's copy of the project's history: a
    pruned one is listed, as no longer kept.  A folder on a computer
    (*device_id*) has records of its own, and its computer holds its
    versions."""
    project = await _project(request, workstream_id, tenant, ctx)
    state = request.app.state
    master = await state.session_store.get_session(project.master_session_id)
    if device_id is None and await over_history_cap(state.storage, master):
        raise HTTPException(status.HTTP_409_CONFLICT, HISTORY_OFF)
    found = await versions(state.session_factory, project.id, path, device_id=device_id, limit=SHELL_LIMITS["versions"])
    kept = None
    if device_id is None:
        try:
            kept = await BucketHistory.of(state.storage, master, state.settings.history).held(v["blob"] for v in found)
        except HistoryError as exc:
            raise _unread(exc) from exc
    for version in found:
        blob = version.pop("blob")
        version["available"] = blob is None or kept is None or blob in kept
    return found


@router.get("/{workstream_id}/history/deleted")
async def deleted_history(workstream_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant) -> dict[str, Any]:
    """The project's files that are gone, the newest first and as many as the
    shell takes: each the version that deleted it, read from the project's
    latest records.  ``more`` says there may be others, deleted before
    these.  The Library lists them under its files, so that a deleted
    file's History is reached.  None in a project over the file cap, whose
    History is off."""
    project = await _project(request, workstream_id, tenant, ctx)
    state = request.app.state
    if await over_history_cap(state.storage, await state.session_store.get_session(project.master_session_id)):
        return {"files": [], "more": False}
    found, more = await deleted_files(state.session_factory, project.id, limit=SHELL_LIMITS["deleted"])
    for version in found:
        del version["blob"]
        version["available"] = True
    return {"files": [version for version in found if units(version["path"]) <= SHELL_LIMITS["ref"]], "more": more}


#: The longest name a file is saved under, in bytes: what a file's name may be nearly everywhere.
_NAME_MOST = 255


def _saved_as(path: str) -> str:
    """The name a version of the file at *path* is saved under: the file's own, as a name and no more.

    Its last part, with no control character and neither kind of slash, cut
    to what a file's name may be with its extension kept.  A path that
    ends in no name is saved as ``file``.
    """
    name = "".join(c for c in path.rsplit("/", 1)[-1].replace("\\", "_") if unicodedata.category(c) != "Cc")
    if not name.strip(". "):
        return "file"
    stem, dot, extension = name.rpartition(".")
    tail = f".{extension}" if stem and len(extension.encode()) <= 16 else ""
    kept = (stem if tail else name).encode()[: _NAME_MOST - len(tail.encode())].decode(errors="ignore")
    return f"{kept}{tail}"


class _VersionFile(Response):
    """A version of a file, sent from the file the api's copy wrote it out to: a piece at a time, and none of it kept after.

    Data to save under the file's own name, whatever it holds: a page or
    a drawing among a project's files is never one a browser shows.  Its
    length is said first, so a version cut short on its way, as for a
    client that takes it slower than a version has to be sent, is a failed
    response to that client and never a shorter file.
    """

    media_type = "application/octet-stream"

    def __init__(self, staged: Staged, path: str) -> None:
        super().__init__(headers={
            "Content-Length": str(staged.size),
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(_saved_as(path), safe='')}",
            "X-Content-Type-Options": "nosniff",
        })
        self.staged = staged

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            await self.staged.send(lambda piece: send({"type": "http.response.body", "body": piece, "more_body": True}))
            await send({"type": "http.response.body", "body": b""})
        finally:
            # A client that left before a word was said took none of it either.
            await self.staged.gone()


@router.get("/{workstream_id}/history/{version}/file")
async def version_file(
    workstream_id: UUID, version: str, path: FilePath, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> Response:
    """Open version: the file at *path* as its version *version* left it,
    to save.  The version is one the project's records name for that file:
    neither *version* nor *path* reaches the storage, or git, which is asked
    only for what the record holds.  Its bytes go from the api's copy of
    the project's history to a file there, and from that file to the
    client a piece at a time."""
    project = await _project(request, workstream_id, tenant, ctx)
    state = request.app.state
    master = await state.session_store.get_session(project.master_session_id)
    if await over_history_cap(state.storage, master):
        raise HTTPException(status.HTTP_409_CONFLICT, HISTORY_OFF)
    entry = await version_of(state.session_factory, project.id, version, path)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such version.")
    if entry["after"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This version deleted the file: there is nothing to open.")
    try:
        staged = await BucketHistory.of(state.storage, master, state.settings.history).version(entry["after"])
    except NotKept as exc:
        raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc
    except (HistoryError, OSError) as exc:
        raise _unread(exc) from exc
    return _VersionFile(staged, path)


class RestoreRequest(BaseModel):
    """A version of a file to restore: one its History lists, by the id it lists it by."""

    version: Annotated[str, StringConstraints(max_length=64)]
    path: FilePath


@router.post("/{workstream_id}/history/restore")
async def restore_version(
    workstream_id: UUID, body: RestoreRequest, request: Request, ctx: AgentRuntime, tenant: Tenant,
    _rate: None = Depends(rate_limit_dep),
) -> dict[str, Any]:
    """Restore: the file at *path* made its version *version* again, as a
    landing by you, your edit to it recorded first: ``{applied, skipped,
    picked_up}``.  The version is one the project's records name for that
    file: neither *version* nor *path* reaches the storage, or git, but
    as the record holds them.  A version that took the file away, or that
    the history keeps no more, is nothing to restore; nor is a file no
    landing writes.  What it waits for and what it refuses is said in words
    (409): the project's files being saved just now, among them."""
    project = await _project(request, workstream_id, tenant, ctx)
    state = request.app.state
    master = await state.session_store.get_session(project.master_session_id)
    if await over_history_cap(state.storage, master):
        raise HTTPException(status.HTTP_409_CONFLICT, HISTORY_OFF)
    entry = await version_of(state.session_factory, project.id, body.version, body.path)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such version.")
    if entry["after"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This version deleted the file: there is nothing to restore.")
    # Before the copy is asked anything of it.
    if not landable(body.path):
        raise HTTPException(status.HTTP_409_CONFLICT, UNLANDED)
    try:
        kept = await BucketHistory.of(state.storage, master, state.settings.history).held([entry["after"]])
    except (HistoryError, OSError) as exc:
        raise _unread(exc) from exc
    if not kept:
        raise HTTPException(status.HTTP_410_GONE, NOT_KEPT)
    try:
        return await restore(state, project, tenant.user_id, path=body.path, blob=entry["after"])
    except Refused as exc:
        raise HTTPException(exc.status, str(exc)) from exc


class UndoRequest(BaseModel):
    """A landing to undo, by the id its History or its thread's row names it by; or every landing of a thread."""

    landing: Annotated[str, StringConstraints(max_length=40)] | None = None
    thread: UUID | None = None

    @model_validator(mode="after")
    def _one(self) -> UndoRequest:
        if (self.landing is None) == (self.thread is None):
            raise ValueError("Name a landing or a thread, not both.")
        return self


@router.post("/{workstream_id}/history/undo")
async def undo_change(
    workstream_id: UUID, body: UndoRequest, request: Request, ctx: AgentRuntime, tenant: Tenant,
    _rate: None = Depends(rate_limit_dep),
) -> dict[str, Any]:
    """Undo: a landing's files, or every landing of a thread, put back as
    they were, as a landing by you, your edits to them recorded first:
    ``{applied, skipped, picked_up}``.  The landing is one of the
    project's own records of its cloud files, and the thread one of its
    live threads: any other is none, whatever its id.  A file changed since
    is left as it is, and named with who changed it.  What it refuses is
    said in words (409): while the thread whose changes these are is
    working, for a change undone already, and the project's files being
    saved just now among them."""
    project = await _project(request, workstream_id, tenant, ctx)
    state = request.app.state
    if await over_history_cap(state.storage, await state.session_store.get_session(project.master_session_id)):
        raise HTTPException(status.HTTP_409_CONFLICT, HISTORY_OFF)
    landing = None
    if body.thread is not None:
        await _thread(request, project, body.thread)
    elif (landing := row_id(body.landing)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_CHANGE)
    try:
        return await undo(state, project, tenant.user_id, landing=landing, thread=body.thread)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except Refused as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/{workstream_id}/stream")
async def stream_project(workstream_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant):
    """The project's changes, as server-sent events: ``ready`` once
    subscribed, so a client that connects again refetches what it missed,
    then a ``change`` for each, naming the thread whose row changed, or
    null when the change is the project's (its master's, or a session's
    under a thread).  The client refetches what it names."""
    project = await _project(request, workstream_id, tenant, ctx)
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis is required for a project's stream.")
    threads = _store(request)

    async def changes():
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(project_stream.channel(project.id))
            yield {"event": "ready", "data": "{}"}
            while not await request.is_disconnected():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    continue
                data = message["data"]
                try:
                    session_id, _, kind = (data.decode() if isinstance(data, bytes) else data).partition(":")
                    changed = UUID(session_id)
                except ValueError:
                    # Only the server publishes here, but Redis takes anyone's.
                    logger.warning("Skipped a malformed change on project %s: %r", project.id, data)
                    continue
                thread = await threads.get_thread(changed)
                yield {"event": "change", "data": json.dumps({
                    "thread_id": session_id if thread is not None and thread.workstream_id == project.id else None,
                    "type": kind,
                })}
        finally:
            # Shielded: a client that leaves cancels the stream, and every
            # await here again, which would keep the connection from its
            # pool.  Closing it ends the subscription.
            with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
                await pubsub.aclose()

    return EventSourceResponse(changes())


async def _thread(request: Request, project: Workstream, thread_id: UUID) -> Session:
    """*thread_id* when it is one of *project*'s live threads; 404 otherwise."""
    row = await _store(request).get_thread(thread_id)
    if row is not None and row.workstream_id == project.id:
        thread = await request.app.state.session_store.get_session(thread_id)
        if thread.status != "archived":
            return thread
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No such thread.")


async def _row(request: Request, project: Workstream, thread_id: UUID) -> dict[str, Any]:
    """*thread_id*'s row, as the threads route gives it."""
    found = await _store(request).thread_facts(project.id, thread_id=thread_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such thread.")
    row = derive_thread(found[0], now=datetime.now(timezone.utc))
    await _say_online(request, [row["place"]])
    return row


@router.post("/{workstream_id}/threads/{thread_id}/resolve")
async def resolve_thread(
    workstream_id: UUID, thread_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> dict[str, Any]:
    """Move a thread to Resolved.  Its work is done, so one still working is
    stopped first, as the coordinator's resolve stops it; one already
    resolved keeps the moment it got there."""
    project = await _project(request, workstream_id, tenant, ctx)
    thread = await _thread(request, project, thread_id)
    state = request.app.state
    await stop_thread(
        thread, reason=_RESOLVED_BY_USER, interrupt=_RESOLVED_BY_USER, session_store=state.session_store,
        session_factory=state.session_factory, redis=state.redis,
    )
    await _store(request).resolve_thread(thread.id)
    await project_stream.publish(state.redis, project.id, thread.id, project_stream.RESOLVED)
    return await _row(request, project, thread.id)


@router.post("/{workstream_id}/threads/{thread_id}/reopen")
async def reopen_thread(
    workstream_id: UUID, thread_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant,
) -> dict[str, Any]:
    """Take a thread out of Resolved, its own or seven quiet days'.  It
    stays as its last turn left it: idle, or waiting on the user."""
    project = await _project(request, workstream_id, tenant, ctx)
    thread = await _thread(request, project, thread_id)
    await _store(request).reopen_thread(thread.id)
    await project_stream.publish(request.app.state.redis, project.id, thread.id, project_stream.REOPENED)
    return await _row(request, project, thread.id)


@contextlib.asynccontextmanager
async def _card_held(request: Request, project: Workstream, proposal_id: UUID, key: str):
    """The card *key* of *proposal_id*, held for one start; Redis, for the start.

    A card started twice at once, by a double click or from two devices:
    the second does not wait for the first, it is refused.  The claim is in
    Redis, so no start holds a database connection while it waits for more
    of the pool, as Start all's every card at once would; the thread's
    ``worker.spawned`` refuses a later start.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis is required to start a thread.")
    claim, token = f"surogates:workstream:card:{project.id}:{proposal_id}:{key}", uuid4().hex
    if not await redis.set(claim, token, nx=True, ex=_CARD_CLAIM_SECONDS):
        raise HTTPException(status.HTTP_409_CONFLICT, "This thread was already started.")
    try:
        if await _store(request).started_from(project.master_session_id, proposal_id, key):
            raise HTTPException(status.HTTP_409_CONFLICT, "This thread was already started.")
        yield redis
    finally:
        # Released however the start ends, so one that failed can be tried
        # again; but only its own, so a start that ran past its claim's
        # expiry leaves the claim of the start that took it over.
        await redis.eval(_RELEASE_CLAIM, 1, claim, token)


async def _card(request: Request, project: Workstream, proposal_id: UUID, key: str) -> dict[str, Any]:
    """The card *key* of the master's proposal *proposal_id*; 404 when there is none."""
    proposed = await _store(request).proposal(project.master_session_id, proposal_id)
    card = next((t for t in (proposed or {}).get("threads", []) if t.get("key") == key), None)
    if card is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such proposed thread.")
    return card


@router.post("/{workstream_id}/threads", status_code=status.HTTP_201_CREATED)
async def start_proposed_thread(
    workstream_id: UUID, body: ProposedThreadStart, request: Request, ctx: AgentRuntime, tenant: Tenant,
    _rate: None = Depends(rate_limit_dep),
) -> dict[str, Any]:
    """Start a thread the master proposed, from its card.  Its title and goal
    are the proposal's, read from the master's log, never the request's; it
    is news to the master, and wakes nobody but itself.

    With *execution*, a folder its user confirmed on their computer, the
    thread is made there and its computer asked to bind it.  It begins once
    bound, from ``…/threads/{thread_id}/start``, and this answers its id.
    """
    project = await _project(request, workstream_id, tenant, ctx)
    # A card proposed for the user's computer runs in the cloud without one:
    # the user chose it there, as a browser's card offers.
    card = await _card(request, project, body.proposal_id, body.key)
    # Checked before anything is made, as a new local-folder chat's is.
    device = None if body.execution is None else await _require_local_device(
        request, tenant, ctx.agent_id, body.execution, channel="web", user_id=tenant.user_id,
    )
    state = request.app.state
    proposal = {"proposal_id": str(body.proposal_id), "key": body.key}
    async with _card_held(request, project, body.proposal_id, body.key) as redis:
        master = await state.session_store.get_session(project.master_session_id)
        if body.execution is None:
            thread = await start_thread(
                session_store=state.session_store, session_factory=state.session_factory, redis=redis,
                master=master, live_config=None, title=card["title"], goal=card["goal"], context="",
                proposal=proposal,
            )
        else:
            thread = await make_thread(
                session_store=state.session_store, session_factory=state.session_factory, master=master,
                live_config=None, title=card["title"], device=device, folder=body.execution.folder, card=proposal,
            )
            if thread is not None:
                await DeviceOperations(state.session_factory, redis).bind(
                    session_id=thread.id, device_id=device.id,
                    folder=body.execution.folder, nonce=body.execution.nonce,
                )
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project.")
    if body.execution is not None:
        return {"thread_id": str(thread.id)}
    return await _row(request, project, thread.id)


@router.post("/{workstream_id}/threads/{thread_id}/start", status_code=status.HTTP_201_CREATED)
async def begin_local_thread(
    workstream_id: UUID, thread_id: UUID, request: Request, ctx: AgentRuntime, tenant: Tenant,
    _rate: None = Depends(rate_limit_dep),
) -> dict[str, Any]:
    """Begin a thread made from a card on the user's computer, once that
    computer has bound it: its row, its goal, the master's news, its queue.
    409 until bound, and for a card started meanwhile."""
    project = await _project(request, workstream_id, tenant, ctx)
    state = request.app.state
    try:
        thread = await state.session_store.get_session(thread_id)
    except SessionNotFoundError:
        thread = None
    made_for = (thread.config or {}).get("workstream_card") if thread is not None else None
    if made_for is None or thread.parent_id != project.master_session_id or thread.status == "archived":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such thread.")
    # 409 until its computer has bound the folder: it has nowhere to work yet.
    await require_device_access(request, thread, tenant)
    proposal_id = UUID(made_for["proposal_id"])
    card = await _card(request, project, proposal_id, made_for["key"])
    async with _card_held(request, project, proposal_id, made_for["key"]) as redis:
        begun = await begin_thread(
            session_store=state.session_store, session_factory=state.session_factory, redis=redis,
            master=await state.session_store.get_session(project.master_session_id), thread=thread,
            title=card["title"], goal=card["goal"], context="", proposal=made_for,
        )
    if begun is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project.")
    return await _row(request, project, thread.id)
