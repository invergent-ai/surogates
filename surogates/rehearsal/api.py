"""The internal API ops calls to start runs and write as their people.

Only ops calls it, with ``rehearsal.internal_token``; ops checks the Studio
user's rights on the agent first and passes the org every call is scoped to.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from surogates.rehearsal import store
from surogates.rehearsal.webhook import post_webhook


def _require_internal_token(request: Request) -> None:
    expected = request.app.state.settings.rehearsal.internal_token
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="Rehearsal internal token required")


router = APIRouter(prefix="/internal", dependencies=[Depends(_require_internal_token)])


class StartRun(BaseModel):
    org_id: uuid.UUID
    agent_id: str
    created_by: str
    person_name: str = Field(min_length=1, max_length=80)
    routing_config: dict[str, Any] = Field(default_factory=dict)


class PersonMessage(BaseModel):
    text: Optional[str] = None
    button_reply: Optional[dict[str, str]] = None
    list_reply: Optional[dict[str, str]] = None


def _message(m) -> dict[str, Any]:
    return {"id": m.id, "direction": m.direction, "kind": m.kind, "body": m.body,
            "status": m.status, "wamid": m.wamid, "created_at": m.created_at.isoformat()}


async def _detail(sf, run) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    people = []
    for person in await store.people(sf, run.id):
        messages = await store.thread(sf, person.id)
        people.append({"id": str(person.id), "name": person.name, "channel": person.channel,
                       "address": person.address, "window_open": store.window_open(messages, now),
                       "agent_typing": store.is_typing(person, now),
                       "messages": [_message(m) for m in messages]})
    return {"id": str(run.id), "agent_id": run.agent_id, "status": run.status,
            "phone_number_id": run.phone_number_id, "created_by": run.created_by,
            "created_at": run.created_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None, "people": people}


async def _run_in_org(request: Request, run_id: uuid.UUID, org_id: uuid.UUID):
    run = await store.get_run(request.app.state.session_factory, run_id)
    if run is None or run.org_id != org_id:
        raise HTTPException(status_code=404, detail="Rehearsal run not found")
    return run


@router.post("/runs", status_code=201)
async def start_run(body: StartRun, request: Request):
    state = request.app.state
    run = await store.create_run(
        state.session_factory, state.vault, org_id=body.org_id, agent_id=body.agent_id,
        created_by=body.created_by, routing_config=body.routing_config,
        person_name=body.person_name.strip(), provision=state.provision,
    )
    return await _detail(state.session_factory, run)


@router.get("/runs")
async def list_runs(org_id: uuid.UUID, agent_id: str, request: Request):
    sf = request.app.state.session_factory
    return [{"id": str(r.id), "status": r.status, "created_by": r.created_by,
             "created_at": r.created_at.isoformat(),
             "people": [p.name for p in await store.people(sf, r.id)]}
            for r in await store.list_runs(sf, org_id=org_id, agent_id=agent_id)]


@router.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, org_id: uuid.UUID, request: Request):
    run = await _run_in_org(request, run_id, org_id)
    return await _detail(request.app.state.session_factory, run)


@router.post("/runs/{run_id}/people/{person_id}/messages")
async def write_as_person(run_id: uuid.UUID, person_id: uuid.UUID, org_id: uuid.UUID,
                          body: PersonMessage, request: Request):
    state = request.app.state
    run = await _run_in_org(request, run_id, org_id)
    if run.status != "running":
        raise HTTPException(status_code=409, detail="This rehearsal has ended")
    person = next((p for p in await store.people(state.session_factory, run.id) if p.id == person_id), None)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person in this rehearsal")
    wamid = f"wamid.R{secrets.token_hex(10)}"
    message: dict[str, Any] = {"from": person.address, "id": wamid,
                               "timestamp": str(int(datetime.now(timezone.utc).timestamp()))}
    if body.button_reply:
        message |= {"type": "interactive",
                    "interactive": {"type": "button_reply", "button_reply": body.button_reply}}
    elif body.list_reply:
        message |= {"type": "interactive",
                    "interactive": {"type": "list_reply", "list_reply": body.list_reply}}
    elif body.text and body.text.strip():
        message |= {"type": "text", "text": {"body": body.text}}
    else:
        raise HTTPException(status_code=422, detail="Write something, or tap a button")
    await store.log_message(state.session_factory, run_id=run.id, person_id=person.id,
                            direction="in", kind=message["type"], body=message, wamid=wamid)
    status = await post_webhook(
        state.http, channels_url=state.settings.rehearsal.channels_url,
        phone_number_id=run.phone_number_id, app_secret=run.app_secret,
        value={"contacts": [{"profile": {"name": person.name}, "wa_id": person.address}],
               "messages": [message]},
    )
    delivered = 200 <= status < 300
    await store.set_status(state.session_factory, run.id, wamid, "delivered" if delivered else "failed")
    return {"delivered": delivered, "channels_status": status}


@router.post("/runs/{run_id}/end")
async def end_run(run_id: uuid.UUID, org_id: uuid.UUID, request: Request):
    await _run_in_org(request, run_id, org_id)
    state = request.app.state
    run = await store.end_run(state.session_factory, state.vault, run_id)
    return await _detail(state.session_factory, run)
