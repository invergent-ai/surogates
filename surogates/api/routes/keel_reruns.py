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
from sqlalchemy.exc import DBAPIError

from surogates.channels.memory_boundary import rerun_kind_of
from surogates.api.routes.keel import (
    agent_id_of,
    body_within,
    case_uuid,
    checked_path,
    create_case_session,
    create_or_find,
    created_answer,
    database_failed,
    kick_off,
    queue_session,
    read_body,
    refused,
    require_link,
    resolve_agent,
    screen,
    session_exists,
    session_where,
    unknown_session,
    write_case_file_answer,
)
from surogates.keel.reruns import (
    RerunRequest,
    mark_rerun,
    may_hold_rerun,
    missing_files,
    read_case_record,
    rerun_config,
    seed_files,
)
from surogates.keel.run_facts import bundle_version_in
from surogates.keel.steps import CASE_STEP_KEY, KICKOFF_MARKER, is_rerun, render_kickoff, step_contract_of
from surogates.session.store import SessionNotFoundError

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
