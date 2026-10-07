"""Projects: the signed-in user's coordinator chats, kept as ``workstreams``.

Each project is a row and its master web session, the coordinator.  Every
route is scoped to the signed-in user and needs the agent's "multi session"
capability: with it off, every web session but the canonical one is hidden.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints, model_validator
from sse_starlette.sse import EventSourceResponse

from surogates.api.routes.sessions import archive_session_tree
from surogates.db.models import Workstream
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep, rate_limit_dep
from surogates.session.models import Session
from surogates.session.provisioning import create_agent_session
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.workstreams import master_config
from surogates.workstreams import stream as project_stream
from surogates.workstreams.derive import SHELL_LIMITS, aware, derive_thread
from surogates.workstreams.store import WorkstreamStore
from surogates.workstreams.threads import start_thread, stop_thread

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
    return [derive_thread(facts, now=now) for facts in found[: SHELL_LIMITS["rows"]]]


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
    return derive_thread(found[0], now=datetime.now(timezone.utc))


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


@router.post("/{workstream_id}/threads", status_code=status.HTTP_201_CREATED)
async def start_proposed_thread(
    workstream_id: UUID, body: ProposedThreadStart, request: Request, ctx: AgentRuntime, tenant: Tenant,
    _rate: None = Depends(rate_limit_dep),
) -> dict[str, Any]:
    """Start a thread the master proposed, from its card.  Its title and goal
    are the proposal's, read from the master's log, never the request's; it
    is news to the master, and wakes nobody but itself."""
    project = await _project(request, workstream_id, tenant, ctx)
    store = _store(request)
    proposed = await store.proposal(project.master_session_id, body.proposal_id)
    card = next((t for t in (proposed or {}).get("threads", []) if t.get("key") == body.key), None)
    if card is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such proposed thread.")
    if card["where"] != "cloud":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This thread works in a folder on your computer: start it from Surogate Desktop.",
        )
    state = request.app.state
    redis = getattr(state, "redis", None)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis is required to start a thread.")
    # A card started twice at once, by a double click or from two devices:
    # the second does not wait for the first, it is refused.  The claim is
    # in Redis, so no start holds a database connection while it waits for
    # more of the pool, as Start all's every card at once would; the
    # thread's ``worker.spawned`` refuses a later start.
    claim, token = f"surogates:workstream:card:{project.id}:{body.proposal_id}:{body.key}", uuid4().hex
    if not await redis.set(claim, token, nx=True, ex=_CARD_CLAIM_SECONDS):
        raise HTTPException(status.HTTP_409_CONFLICT, "This thread was already started.")
    try:
        if await store.started_from(project.master_session_id, body.proposal_id, body.key):
            raise HTTPException(status.HTTP_409_CONFLICT, "This thread was already started.")
        thread = await start_thread(
            session_store=state.session_store, session_factory=state.session_factory, redis=redis,
            master=await state.session_store.get_session(project.master_session_id), live_config=None,
            title=card["title"], goal=card["goal"], context="",
            proposal={"proposal_id": str(body.proposal_id), "key": body.key},
        )
    finally:
        # Released however the start ends, so one that failed can be tried
        # again; but only its own, so a start that ran past its claim's
        # expiry leaves the claim of the start that took it over.
        await redis.eval(_RELEASE_CLAIM, 1, claim, token)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project.")
    return await _row(request, project, thread.id)
