"""Keel's routes for a rerun or a trial: through ``keel-link``, Keel starts an agent's step again, on the files a
past step read as they were or on inputs a builder typed, gives a trial its files, and starts it.

A rerun's session is a step session of a case that exists nowhere else (:mod:`surogates.keel.reruns`): on ``api``,
owned by the case type's account, in a boundary of its own id, queued behind every case's work, and without a
terminal or a browser.  The same link key, errors and keys as the step routes (:mod:`surogates.api.routes.keel`),
whose helpers these routes share.

Mounted only on the keel-link app, never on the public API.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.exc import DBAPIError

from surogates.channels.memory_boundary import CASE_RERUN_KEY, rerun_kind_of
from surogates.api.routes.keel import (
    StepSessionStatus,
    agent_id_of,
    body_within,
    cancel_work_session,
    case_uuid,
    checked_path,
    create_case_session,
    create_or_find,
    created_answer,
    database_failed,
    database_unavailable,
    events_page,
    events_stream,
    kick_off,
    progress_answer,
    queue_session,
    read_body,
    refused,
    require_link,
    resolve_agent,
    screen,
    session_exists,
    session_where,
    unknown_session,
    watched_session,
    write_case_file_answer,
)
from surogates.keel.case_files import case_workspace
from surogates.keel.reruns import (
    RerunRequest,
    delete_boundary,
    mark_rerun,
    may_hold_rerun,
    missing_files,
    read_case_record,
    rerun_config,
    rerun_memory_keys,
    seed_files,
)
from surogates.keel.run_facts import bundle_version_in
from surogates.keel.steps import CASE_STEP_KEY, KICKOFF_MARKER, is_rerun, render_kickoff, step_contract_of
from surogates.office.sessions import forget_closed_under, open_under
from surogates.session.store import RerunRunning, SessionNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter()


def _unavailable(exc: Exception, session_id: UUID | str) -> HTTPException:
    """The 503 for a store that would not answer, logged by the session's id and the error's class name.

    The error's text can quote an object's key: a case's file name.
    """
    logger.warning("keel-link: the files of rerun %s could not be reached (%s)", session_id, type(exc).__name__)
    return refused(
        status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "The rerun's files could not be reached; try again.",
    )


def _case_exists() -> HTTPException:
    return refused(
        status.HTTP_409_CONFLICT, "case-exists", "The id names a case, or another org's or kind's rerun.",
    )


def _files_gone(missing: list[int]) -> HTTPException:
    error = refused(
        status.HTTP_409_CONFLICT, "file-version-gone", "A version of the files the rerun starts with is gone.",
    )
    error.detail["missing"] = missing
    return error


@router.post("/keel/reruns")
async def create_rerun(request: Request) -> JSONResponse:
    """Start a rerun or a trial of the agent's step: 201 made, or 200 found by its key.

    Its files are copied before it answers, from the versions the case they came from keeps, and nothing is
    written, no session, no mark and no file, for one whose version is gone: 409 ``file-version-gone``, naming the
    files.
    """
    require_link(request)
    body = await read_body(request, RerunRequest)
    agent_id = agent_id_of(request)
    agent = await resolve_agent(request, agent_id, body.org_id)
    paths = [checked_path(request, body.session_id, file.path) for file in body.files]
    if len(set(paths)) != len(paths):
        # Two names for one path, once its dots are dropped.
        raise refused(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "Two files name one path.")
    screen(body.contract.inputs, case_id=body.session_id)
    contract = body.contract if body.mcp else body.contract.model_copy(update={"mcp_servers": []})
    state = request.app.state

    def this_rerun(existing: Any) -> bool:
        return existing.agent_id == agent_id and is_rerun(existing) and rerun_kind_of(existing) == body.kind

    async def start(session: Any) -> None:
        if body.start:
            await kick_off(
                request, session, content=render_kickoff(contract), synthetic=KICKOFF_MARKER, key=body.idempotency_key,
            )

    async def make() -> Any:
        if await session_exists(state.session_store, body.session_id):
            raise refused(
                status.HTTP_409_CONFLICT, "key-in-use", "The key or the session id names another session.",
            )
        record = await read_case_record(state.storage, state.settings, body.session_id)
        if not may_hold_rerun(record, body.org_id, body.kind):
            raise _case_exists()
        if body.of_case_id is not None:
            gone = await missing_files(
                state.storage, state.settings, of_case_id=body.of_case_id, org_id=body.org_id, files=body.files,
            )
            if gone:
                raise _files_gone(gone)
        if record is None:
            await mark_rerun(state.storage, state.settings, body.session_id, body.org_id, body.kind)
        if body.of_case_id is not None:
            gone = await seed_files(
                state.storage, state.settings, of_case_id=body.of_case_id, case_id=body.session_id, files=body.files,
            )
            if gone:
                raise _files_gone(gone)
        return await create_case_session(
            request,
            body.model_copy(update={"contract": contract}),
            agent_id,
            file_pane=contract.mode == "draft",
            case_step=contract.model_dump(mode="json"),
            case_rerun=rerun_config(body.kind, agent),
        )

    try:
        session, created = await create_or_find(request, body, matches=this_rerun, make=make, start=start)
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the rerun {body.session_id} could not be written",
            message="The rerun could not be created.",
        ) from None
    except (ClientError, BotoCoreError) as exc:
        raise _unavailable(exc, body.session_id) from None
    return created_answer(session, created=created, bundle_version=bundle_version_in(agent.get("bundle_version")))


def _org_of(request: Request) -> UUID:
    """The ``org_id`` query parameter: 422 ``invalid-request`` when it is missing or not a UUID."""
    try:
        return UUID(request.query_params.get("org_id") or "")
    except ValueError:
        raise refused(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid-request", "The org id is not a UUID.",
        ) from None


@router.put("/keel/reruns/{session_id}/files/content")
async def write_trial_file(session_id: str, request: Request) -> JSONResponse:
    """Write the body at the path in a trial's workspace, before or after the trial is created: the file's Keel
    value and size, as a case's file write answers.

    The first write marks the folder as the org's trial's, so nothing can tell it from a case's and take a case's
    files away.  A folder that is a case's, another org's or a rerun's is 409 ``case-exists``.
    """
    require_link(request)
    trial = case_uuid(session_id)
    org_id = _org_of(request)
    relative = checked_path(request, trial, request.query_params.get("path") or "")
    data = await body_within(request, request.app.state.settings.keel.max_file_bytes)
    state = request.app.state
    try:
        record = await read_case_record(state.storage, state.settings, trial)
        if not may_hold_rerun(record, org_id, "trial"):
            raise _case_exists()
        if record is None:
            await mark_rerun(state.storage, state.settings, trial, org_id, "trial")
    except (ClientError, BotoCoreError) as exc:
        raise _unavailable(exc, trial) from None
    return await write_case_file_answer(request, trial, relative, data)


@router.post("/keel/reruns/{session_id}/start")
async def start_rerun(session_id: str, request: Request) -> JSONResponse:
    """Write a rerun's first message, once, and queue it: ``202 {"started": bool}``, true when this call wrote it.

    A rerun that was cancelled is over: 409 ``step-finished``.
    """
    require_link(request)
    try:
        session = await session_where(request, session_id, is_rerun)
        contract = session.config.get(CASE_STEP_KEY)
        if isinstance(contract, Mapping) and contract.get("inputs_redacted") is True:
            raise refused(status.HTTP_409_CONFLICT, "step-finished", "The rerun is over; it takes no more messages.")
        written = await request.app.state.session_store.emit_keyed_synthetic_message(
            session.id,
            content=render_kickoff(step_contract_of(session)),
            synthetic=KICKOFF_MARKER,
            key=session.idempotency_key,
        )
    except SessionNotFoundError:
        # Deleted since the lookup above.
        raise unknown_session() from None
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the rerun {session_id} could not be started",
            message="The rerun could not be started.",
        ) from None
    if written is not None:
        await queue_session(request, session)
    return JSONResponse({"started": written is not None}, status_code=status.HTTP_202_ACCEPTED)


@router.get("/keel/reruns/{session_id}")
async def read_rerun(session_id: str, request: Request) -> JSONResponse:
    """A rerun's status, and when it last changed."""
    require_link(request)
    session = await watched_session(request, session_id, is_rerun)
    answer = StepSessionStatus(status=session.status, updated_at=session.updated_at)
    return JSONResponse(answer.model_dump(mode="json"))


@router.get("/keel/reruns/{session_id}/events")
async def read_rerun_events(session_id: str, request: Request) -> JSONResponse:
    """A page of a rerun's events after the cursor, as a step's page."""
    require_link(request)
    return await events_page(request, await watched_session(request, session_id, is_rerun))


@router.get("/keel/reruns/{session_id}/events/stream")
async def stream_rerun_events(session_id: str, request: Request) -> EventSourceResponse:
    """A rerun's events after the cursor, then each new one as it is written, as a step's stream."""
    require_link(request)
    return await events_stream(request, await watched_session(request, session_id, is_rerun))


@router.get("/keel/reruns/{session_id}/progress")
async def read_rerun_progress(session_id: str, request: Request) -> JSONResponse:
    """A rerun's status, its todo list as its agent last wrote it, and its last activity."""
    require_link(request)
    return await progress_answer(request, await watched_session(request, session_id, is_rerun))


@router.post("/keel/reruns/{session_id}/cancel")
async def cancel_rerun(session_id: str, request: Request) -> JSONResponse:
    """End a rerun: remove its inputs, pause it and interrupt its turn: ``200 {"status": "paused"}``, also when it
    was paused already."""
    require_link(request)
    try:
        session = await session_where(request, session_id, is_rerun)
    except DBAPIError as exc:
        raise database_failed(
            exc,
            logged=f"the rerun {session_id} could not be cancelled",
            message="The rerun could not be cancelled.",
        ) from None
    return await cancel_work_session(request, session)


def _running() -> HTTPException:
    return refused(
        status.HTTP_409_CONFLICT, "rerun-running",
        "A session of the rerun is in a turn, or one of its documents is open: cancel it, then purge it later.",
    )


@router.delete("/keel/reruns/{session_id}")
async def purge_rerun(session_id: str, request: Request) -> JSONResponse:
    """Delete a rerun or a trial whole: its memory, its sessions with every row that points at them, and its
    boundary folder: ``200 {"sessions", "objects"}``.

    In that order, the folder's record last: a purge that failed part-way is finished by a retry, which finds the
    mark, and a purge of what is gone answers zeros.  An id that names a session that is no rerun's, or a folder
    recorded as a case's, is 404 ``unknown-session`` and nothing is deleted.  A rerun in a turn, or with a document
    open in the editor, is 409 ``rerun-running``: a save landing after the purge would write under the folder again.
    """
    require_link(request)
    try:
        rerun = UUID(session_id)
    except ValueError:
        raise unknown_session() from None
    state = request.app.state
    try:
        try:
            session = await state.session_store.get_session(rerun)
        except SessionNotFoundError:
            session = None
        except DBAPIError as exc:
            raise database_unavailable(
                exc, logged=f"the rerun {session_id} could not be read", message="The rerun could not be read; try again.",
            ) from None
        if session is not None and not is_rerun(session):
            raise unknown_session()
        record = await read_case_record(state.storage, state.settings, rerun)
        if record is not None and record.rerun is None:
            # A case's folder: never a rerun's to delete.
            raise unknown_session()
        if session is None and record is None:
            return JSONResponse({"sessions": 0, "objects": 0})
        workspace = case_workspace(state.storage, state.settings, rerun).location("")
        try:
            if session is not None:
                for member in await state.session_store.session_tree_ids(rerun):
                    if await state.session_store.has_live_lease(member):
                        raise _running()
            if await open_under(state.session_factory, workspace):
                raise _running()
            sessions = 0
            if session is not None:
                memory_prefix = (session.config.get(CASE_RERUN_KEY) or {}).get("memory_prefix")
                if memory_prefix is not None:
                    bucket = state.settings.storage.memory_bucket or state.settings.storage.bucket
                    for key in rerun_memory_keys(memory_prefix, rerun):
                        await state.storage.delete(bucket, key)
                try:
                    sessions = await state.session_store.delete_rerun_tree(rerun)
                except RerunRunning:
                    raise _running() from None
                except SessionNotFoundError:
                    sessions = 0
            await forget_closed_under(state.session_factory, workspace)
        except DBAPIError as exc:
            raise database_failed(
                exc, logged=f"the rerun {session_id} could not be purged", message="The rerun could not be purged.",
            ) from None
        objects = await delete_boundary(state.storage, state.settings, rerun)
    except (ClientError, BotoCoreError) as exc:
        raise _unavailable(exc, rerun) from None
    logger.info("keel-link: rerun %s purged (%d sessions, %d objects)", rerun, sessions, objects)
    return JSONResponse({"sessions": sessions, "objects": objects})
