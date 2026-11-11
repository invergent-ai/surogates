"""The internal API ops calls to start runs and write as their people.

Only ops calls it, with ``simulation.internal_token``; ops checks the Studio
user's rights on the agent first and passes the org every call is scoped to.
"""

from __future__ import annotations

import functools
import json
import logging

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from surogates.api.routes.ask_user_question import AskUserQuestionResponseRequest
from surogates.db.models import ProgramInvitationRow, ProgramScheduleRow
from surogates.simulation import store
from surogates.simulation import discord, slack, teams, voice, website
from surogates.simulation.webhook import (
    post_slack,
    post_teams_activity,
    post_telegram_update,
    post_webhook,
)


def _require_internal_token(request: Request) -> None:
    expected = request.app.state.settings.simulation.internal_token
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="Simulation internal token required")


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", dependencies=[Depends(_require_internal_token)])


class PlanPerson(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    about: str = Field(min_length=1, max_length=1000)
    test: str = Field(min_length=1, max_length=500)
    #: A team member this person stands in for (their real user id); their rights come with it.
    member_id: Optional[uuid.UUID] = None
    #: False: the user plays them by hand (voice always).
    autopilot: bool = True


class StartRun(BaseModel):
    org_id: uuid.UUID
    agent_id: str
    created_by: str
    batch_id: uuid.UUID
    channel: Literal["whatsapp", "telegram", "slack", "teams", "discord", "website", "voice"] = "whatsapp"
    people: list[PlanPerson] = Field(min_length=1, max_length=8)
    routing_config: dict[str, Any] = Field(default_factory=dict)
    templates: list[dict[str, Any]] = Field(default_factory=list)
    flows: list[dict[str, Any]] = Field(default_factory=list)
    via: Optional[Literal["copilot"]] = None


class QuestionAnswer(AskUserQuestionResponseRequest):
    """A website visitor's answers to one ``ask_user_question`` call."""

    tool_call_id: str = Field(min_length=1, max_length=200)


class PersonMessage(BaseModel):
    text: Optional[str] = None
    button_reply: Optional[dict[str, str]] = None
    list_reply: Optional[dict[str, str]] = None
    #: A template's quick-reply button: ``{"text", "payload"}``.
    quick_reply: Optional[dict[str, str]] = None
    #: A completed WhatsApp Flow: the ``complete`` payload, ``flow_token`` included.
    form_reply: Optional[dict[str, Any]] = None
    #: A Telegram inline-keyboard tap: ``{"message_id", "data"}``.
    callback: Optional[dict[str, str]] = None
    #: A Slack Block Kit button click: ``{"ts", "action_id"}``.
    action: Optional[dict[str, str]] = None
    #: A Slack modal: ``{"id", "values": {block_id: answer}}`` submits it,
    #: ``{"id", "cancel": true}`` closes it.
    view: Optional[dict[str, Any]] = None
    #: A website visitor answering the agent's questions.
    answer: Optional[QuestionAnswer] = None


def _message(m) -> dict[str, Any]:
    return {"id": m.id, "direction": m.direction, "kind": m.kind, "body": m.body,
            "status": m.status, "wamid": m.wamid, "created_at": m.created_at.isoformat()}


async def _detail(sf, run) -> dict[str, Any]:
    from surogates.followup.store import get_settings, member_permissions

    now = datetime.now(timezone.utc)
    people, user_ids = [], []
    found = await store.people(sf, run.id)
    settings = (await get_settings(sf, org_id=run.org_id, agent_id=run.agent_id)
                if any(p.stands_for for p in found) else None)
    enrolled = await store.enrolled(sf, run, [p.user_id for p in found if p.user_id])
    for person in found:
        if person.user_id:
            user_ids.append(person.user_id)
        messages = await store.thread(sf, person.id)
        rights = member_permissions(settings, person.stands_for) if person.stands_for else None
        people.append({"id": str(person.id), "name": person.name, "channel": person.channel,
                       "team": person.stands_for is not None,
                       "user_id": str(person.user_id) if person.user_id else None,
                       "task": person.task,
                       "stands_for": str(person.stands_for) if person.stands_for else None,
                       "member_id": str(person.stands_for) if person.stands_for else None,
                       "about": person.about, "test": person.test,
                       "enrolled": person.user_id in enrolled if person.user_id else False,
                       "permissions": rights and {"receives": sorted(rights["receives"]),
                                                  "can": sorted(rights["can"]),
                                                  "handles": rights["handles"]},
                       # A visitor's address holds their widget session's cookie.
                       "address": "visitor" if person.channel == "website" else person.address,
                       "persona": person.persona,
                       "driver": person.driver, "ai_state": person.ai_state,
                       "ai_note": person.ai_note,
                       "window_open": store.window_open(messages, now),
                       "agent_typing": store.is_typing(person, now),
                       "messages": [_message(m) for m in messages]})
    return {"id": str(run.id), "agent_id": run.agent_id, "status": run.status,
            "channel": run.channel, "batch_id": str(run.batch_id) if run.batch_id else None,
            "phone_number_id": run.phone_number_id, "created_by": run.created_by,
            "via": run.via, "created_at": run.created_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None, "people": people,
            "flows": run.flows or [],
            "review": run.review,
            "team": await store.team_messages(sf, user_ids)}


async def _run_in_org(request: Request, run_id: uuid.UUID, org_id: uuid.UUID):
    run = await store.get_run(request.app.state.session_factory, run_id)
    if run is None or run.org_id != org_id:
        raise HTTPException(status_code=404, detail="Simulation run not found")
    return run


@router.get("/channels")
async def channels(request: Request):
    """The channels a run can be started on here: a disabled adapter would never answer."""
    settings = request.app.state.settings
    found = ["whatsapp", "website"]
    found += [k for k in ("telegram", "slack", "teams", "discord")
              if getattr(settings.channels, k).enabled]
    if settings.voice.livekit_api_key:
        found.append("voice")
    return {"channels": found}


@router.post("/runs", status_code=201)
async def start_run(body: StartRun, request: Request):
    from surogates.followup.store import get_settings

    state = request.app.state
    named = [p.member_id for p in body.people if p.member_id]
    if len(named) != len(set(named)):
        raise HTTPException(status_code=422, detail="A team member can stand in only once per run")
    members = set(named)
    if members:
        if body.channel not in AI_CHANNELS or body.channel == "website":
            raise HTTPException(status_code=422, detail="Team members chat with the agent on chat channels only")
        settings = await get_settings(state.session_factory, org_id=body.org_id, agent_id=body.agent_id)
        team = {str(m) for m in (settings.alert_recipient_user_ids or [])} if settings else set()
        if not {str(m) for m in members} <= team:
            raise HTTPException(status_code=422, detail="Someone in the plan is not on this agent's team")
    autopilot = body.channel in AI_CHANNELS
    run = await store.create_run(
        state.session_factory, state.vault, org_id=body.org_id, agent_id=body.agent_id,
        created_by=body.created_by, routing_config=body.routing_config, provision=state.provision,
        templates=body.templates, flows=body.flows, channel=body.channel, batch_id=body.batch_id,
        people=[{**p.model_dump(), "name": p.name.strip(), "autopilot": p.autopilot and autopilot}
                for p in body.people],
        via=body.via,
    )
    return await _detail(state.session_factory, run)


def _changes(run, people) -> list[dict[str, Any]]:
    """What the run's review changed in the agent, each with its finding's index for Undo."""
    tests = {str(p.id): p.test for p in people}
    return [
        {"run_id": str(run.id), "index": index, "title": f.get("title"),
         "target": (f.get("proposal") or {}).get("target"),
         "name": (f.get("proposal") or {}).get("name"), "person_id": f.get("person_id"),
         "test": tests.get(f.get("person_id") or ""), "applied_at": f.get("applied_at")}
        for index, f in enumerate((run.review or {}).get("findings") or [])
        if f.get("state") == "applied"
    ]


@router.get("/runs")
async def list_runs(org_id: uuid.UUID, agent_id: str, request: Request):
    sf = request.app.state.session_factory
    runs = await store.list_runs(sf, org_id=org_id, agent_id=agent_id)
    counts = await store.message_counts(sf, [r.id for r in runs])
    everyone = await store.people_of(sf, [r.id for r in runs])
    summaries = []
    for r in runs:
        people = everyone[r.id]
        first = people[0] if people else None
        summaries.append({
            "id": str(r.id), "status": r.status, "channel": r.channel,
            "batch_id": str(r.batch_id) if r.batch_id else None,
            "created_by": r.created_by, "via": r.via,
            "created_at": r.created_at.isoformat(),
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "people": [p.name for p in people if p.stands_for is None],
            "persona": first.persona if first else None,
            "driver": first.driver if first else None,
            "ai_state": first.ai_state if first else None,
            "ai_note": first.ai_note if first else None,
            "messages": counts.get(r.id, 0),
            "verdict": (r.review or {}).get("verdict"),
            "changes": _changes(r, people),
        })
    return summaries


@router.get("/batches")
async def list_batches(org_id: uuid.UUID, agent_id: str, request: Request):
    """Simulations newest first: the runs one plan started, grouped, with each person's grade."""
    sf = request.app.state.session_factory
    # ponytail: batches come from the newest 200 runs; page by batch if plans grow past that.
    runs = await store.list_runs(sf, org_id=org_id, agent_id=agent_id, limit=200)
    everyone = await store.people_of(sf, [r.id for r in runs])
    batches: dict[uuid.UUID, list] = {}
    for r in runs:
        batches.setdefault(r.batch_id or r.id, []).append(r)
    listed = []
    for batch_id, group in list(batches.items())[:50]:
        group.reverse()
        people, changes = [], []
        for r in group:
            grades = {g.get("person_id"): g for g in (r.review or {}).get("grades") or []}
            changes += _changes(r, everyone[r.id])
            for p in everyone[r.id]:
                grade = grades.get(str(p.id))
                people.append({
                    "name": p.name, "channel": p.channel, "about": p.about, "test": p.test,
                    "member_id": str(p.stands_for) if p.stands_for else None,
                    "ai_state": p.ai_state,
                    "grade": grade and {"passed": bool(grade.get("passed")),
                                        "reason": grade.get("reason"),
                                        "message_id": grade.get("message_id")},
                })
        graded = [p["grade"] for p in people if p["grade"]]
        listed.append({
            "id": str(batch_id), "created_at": group[-1].created_at.isoformat(),
            "status": "running" if any(r.status == "running" for r in group) else "ended",
            "channels": list(dict.fromkeys(r.channel for r in group)),
            "people": people, "passed": sum(g["passed"] for g in graded), "graded": len(graded),
            "total": len(people), "via": group[0].via, "changes": changes,
        })
    return listed


async def _batch(request: Request, batch_id: uuid.UUID, org_id: uuid.UUID, agent_id: str):
    runs = await store.batch_runs(request.app.state.session_factory, org_id=org_id,
                                  agent_id=agent_id, batch_id=batch_id)
    if not runs:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return runs


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: uuid.UUID, org_id: uuid.UUID, agent_id: str, request: Request):
    sf = request.app.state.session_factory
    runs = await _batch(request, batch_id, org_id, agent_id)
    return {"id": str(batch_id), "runs": [await _detail(sf, r) for r in runs]}


@router.post("/batches/{batch_id}/end")
async def end_batch(batch_id: uuid.UUID, org_id: uuid.UUID, agent_id: str, request: Request):
    state = request.app.state
    runs = await _batch(request, batch_id, org_id, agent_id)
    for r in runs:
        if r.status == "running":
            await store.finish_run(state, r.id)
    return await get_batch(batch_id, org_id, agent_id, request)


@router.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, org_id: uuid.UUID, request: Request):
    run = await _run_in_org(request, run_id, org_id)
    return await _detail(request.app.state.session_factory, run)


@router.put("/runs/{run_id}/review")
async def put_review(run_id: uuid.UUID, org_id: uuid.UUID, review: dict[str, Any], request: Request):
    run = await _run_in_org(request, run_id, org_id)
    sf = request.app.state.session_factory
    await store.set_review(sf, run.id, review)
    run.review = review
    return await _detail(sf, run)


@router.post("/runs/{run_id}/people/{person_id}/messages")
async def write_as_person(run_id: uuid.UUID, person_id: uuid.UUID, org_id: uuid.UUID,
                          body: PersonMessage, request: Request):
    state = request.app.state
    run = await _run_in_org(request, run_id, org_id)
    person = await _running_person(state, run, person_id)
    return await write(state, run, person, body)


async def _running_person(state, run, person_id: uuid.UUID):
    if run.status != "running":
        raise HTTPException(status_code=409, detail="This simulation has ended")
    person = next((p for p in await store.people(state.session_factory, run.id) if p.id == person_id), None)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person in this simulation")
    return person


class Driver(BaseModel):
    driver: Literal["ai", "human"]


@router.put("/runs/{run_id}/people/{person_id}/driver")
async def set_driver(run_id: uuid.UUID, person_id: uuid.UUID, org_id: uuid.UUID, body: Driver,
                     request: Request):
    """Take over from the AI, or hand back to it: it carries on from where the chat is."""
    state = request.app.state
    run = await _run_in_org(request, run_id, org_id)
    person = await _running_person(state, run, person_id)
    if not person.persona:
        raise HTTPException(status_code=422, detail="Autopilot needs to know who this person is")
    if body.driver == "ai" and run.channel not in AI_CHANNELS:
        raise HTTPException(status_code=422, detail="Autopilot writes on chat channels only")
    values: dict[str, Any] = {"driver": body.driver}
    if body.driver == "ai":
        # Handing back restarts the AI, whatever stopped it.
        values |= {"ai_state": "talking", "ai_note": None, "ai_acted_at": None}
    await store.update_person(state.session_factory, person.id, **values)
    return await _detail(state.session_factory, run)


#: The channels the AI can write on: a phone call needs a voice.
AI_CHANNELS = ("whatsapp", "telegram", "slack", "teams", "discord", "website")


async def write(state, run, person, body: PersonMessage) -> dict[str, Any]:
    """The person's message or tap, delivered as their platform would."""
    if run.channel == "website":
        given = body.model_dump(exclude_none=True).keys()
        if given == {"answer"}:
            return await website.answer(state, run, person, body.answer.tool_call_id,
                                        [r.model_dump() for r in body.answer.responses])
        if given != {"text"} or not body.text.strip():
            raise HTTPException(status_code=422, detail="On a website the visitor writes text or answers a question")
        return await website.send(state, run, person, body.text)
    if run.channel == "voice":
        raise HTTPException(status_code=422, detail="On a phone line the caller speaks: place a call")
    if run.channel == "telegram":
        return await _write_on_telegram(state, run, person, body)
    if run.channel == "slack":
        return await _write_on_slack(state, run, person, body)
    if run.channel == "teams":
        return await _write_on_teams(state, run, person, body)
    if run.channel == "discord":
        return await _write_on_discord(state, run, person, body)
    wamid = f"wamid.R{secrets.token_hex(10)}"
    message: dict[str, Any] = {"from": person.address, "id": wamid,
                               "timestamp": str(int(datetime.now(timezone.utc).timestamp()))}
    if body.button_reply:
        message |= {"type": "interactive",
                    "interactive": {"type": "button_reply", "button_reply": body.button_reply}}
    elif body.form_reply:
        message |= {"type": "interactive", "interactive": {
            "type": "nfm_reply",
            "nfm_reply": {"name": "flow", "body": "Sent",
                          "response_json": json.dumps(body.form_reply, ensure_ascii=False)}}}
    elif body.quick_reply:
        message |= {"type": "button", "button": body.quick_reply}
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
        state.http, channels_url=state.settings.simulation.channels_url,
        phone_number_id=run.phone_number_id, app_secret=run.app_secret,
        value={"contacts": [{"profile": {"name": person.name}, "wa_id": person.address}],
               "messages": [message]},
    )
    delivered = 200 <= status < 300
    await store.set_status(state.session_factory, run.id, wamid, "delivered" if delivered else "failed")
    return {"delivered": delivered, "channels_status": status}


async def _write_on_telegram(state, run, person, body: PersonMessage) -> dict[str, Any]:
    """The person's message or button tap, as the Update Telegram would POST."""
    sf = state.session_factory
    sender = {"id": int(person.address), "is_bot": False, "first_name": person.name}
    chat = {"id": int(person.address), "type": "private", "first_name": person.name}
    now = int(datetime.now(timezone.utc).timestamp())
    update_id = await store.next_message_id(sf, run.id)
    if body.callback:
        tapped = await store.message(sf, run.id, str(body.callback.get("message_id") or ""))
        if tapped is None or tapped.direction != "out":
            raise HTTPException(status_code=404, detail="No such message to tap")
        update = {"update_id": update_id, "callback_query": {
            "id": secrets.token_hex(8), "from": sender, "chat_instance": person.address,
            "message": tapped.body, "data": str(body.callback.get("data") or ""),
        }}
        status = await post_telegram_update(
            state.http, channels_url=state.settings.simulation.channels_url,
            bot_username=run.phone_number_id, webhook_secret=run.app_secret, update=update,
        )
        return {"delivered": 200 <= status < 300, "channels_status": status}
    if not (body.text and body.text.strip()):
        raise HTTPException(status_code=422, detail="Write something, or tap a button")
    message = {"message_id": update_id, "from": sender, "chat": chat, "date": now,
               "text": body.text}
    wamid = str(update_id)
    await store.log_message(sf, run_id=run.id, person_id=person.id, direction="in", kind="text",
                            body=message, wamid=wamid)
    status = await post_telegram_update(
        state.http, channels_url=state.settings.simulation.channels_url,
        bot_username=run.phone_number_id, webhook_secret=run.app_secret,
        update={"update_id": update_id, "message": message},
    )
    delivered = 200 <= status < 300
    # A bot takes a message the moment Telegram hands it over: two ticks.
    await store.set_status(sf, run.id, wamid, "read" if delivered else "failed")
    return {"delivered": delivered, "channels_status": status}


def _modal_state(view: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """Slack's ``view.state.values`` for *answers* keyed by block_id: an option value or text."""
    values: dict[str, Any] = {}
    for block in view.get("blocks") or []:
        element = block.get("element") or {}
        if block.get("type") != "input" or not element.get("action_id"):
            continue
        answer = answers.get(block.get("block_id"))
        if element.get("type") == "static_select":
            option = next((o for o in element.get("options") or [] if o.get("value") == answer), None)
            values[block["block_id"]] = {element["action_id"]: {
                "type": "static_select", "selected_option": option}}
        else:
            values[block["block_id"]] = {element["action_id"]: {
                "type": element.get("type") or "plain_text_input",
                "value": None if answer in (None, "") else str(answer)}}
    return values


async def _write_on_discord(state, run, person, body: PersonMessage) -> dict[str, Any]:
    """The member's DM, as the Gateway dispatches it to the bot."""
    if not (body.text and body.text.strip()):
        raise HTTPException(status_code=422, detail="Write something")
    message_id = discord.new_snowflake()
    message = {
        "id": message_id, "type": 0, "channel_id": discord.dm_of(person.address),
        "author": discord.author(person), "content": body.text, "mentions": [],
        "attachments": [], "embeds": [], "timestamp": datetime.now(timezone.utc).isoformat(),
        "edited_timestamp": None,
    }
    await store.log_message(state.session_factory, run_id=run.id, person_id=person.id,
                            direction="in", kind="message", body=message, wamid=message_id,
                            status="pending")
    delivered = await discord.GATEWAYS.dispatch(run.id, message)
    if delivered:
        await store.set_status(state.session_factory, run.id, message_id, "sent")
    return {"delivered": delivered}


async def _write_on_teams(state, run, person, body: PersonMessage) -> dict[str, Any]:
    """The member's message in their personal chat with the bot, as Bot Framework delivers it."""
    if not (body.text and body.text.strip()):
        raise HTTPException(status_code=422, detail="Write something")
    sf = state.session_factory
    service = teams.service_url(state.settings)
    activity_id = f"1{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    tenant = teams.tenant_of(run)
    activity = {
        "type": "message", "id": activity_id, "channelId": "msteams", "serviceUrl": service,
        "timestamp": datetime.now(timezone.utc).isoformat(), "textFormat": "plain",
        "from": {"id": person.address, "name": person.name},
        "recipient": {"id": f"28:{run.phone_number_id}", "name": "agent"},
        "conversation": {"id": teams.conversation_of(person.address),
                         "conversationType": "personal", "tenantId": tenant},
        "channelData": {"tenant": {"id": tenant}},
        "text": body.text,
    }
    await store.log_message(sf, run_id=run.id, person_id=person.id, direction="in",
                            kind="message", body=activity, wamid=activity_id)
    status = await post_teams_activity(
        state.http, channels_url=state.settings.simulation.channels_url,
        app_id=run.phone_number_id, token=teams.sign(run.phone_number_id, service),
        activity=activity,
    )
    delivered = 200 <= status < 300
    await store.set_status(sf, run.id, activity_id, "sent" if delivered else "failed")
    return {"delivered": delivered, "channels_status": status}


async def _write_on_slack(state, run, person, body: PersonMessage) -> dict[str, Any]:
    """The member's message, button click or modal, as Slack would deliver it."""
    sf = state.session_factory
    team = {"id": slack.team_id(run), "domain": "simulation"}
    user = {"id": person.address, "username": person.name.lower(), "name": person.name.lower(),
            "team_id": team["id"]}
    deliver = functools.partial(
        post_slack, state.http, channels_url=state.settings.simulation.channels_url,
        app_id=run.phone_number_id, signing_secret=run.app_secret,
    )
    trigger = f"{person.address}.{secrets.token_hex(6)}"

    def interaction(payload: dict[str, Any]):
        raw = urlencode({"payload": json.dumps(payload)}).encode()
        return deliver(path="/interact", raw=raw, content_type="application/x-www-form-urlencoded")

    if body.view:
        opened = await store.message(sf, run.id, str(body.view.get("id") or ""))
        if opened is None or opened.kind != "view" or opened.status != "open":
            raise HTTPException(status_code=404, detail="No open modal")
        if body.view.get("cancel"):
            await store.set_status(sf, run.id, opened.wamid, "closed")
            return {"delivered": True, "channels_status": 200}
        view = opened.body["view"]
        submitted = {**view, "state": {"values": _modal_state(view, body.view.get("values") or {})}}
        status = await interaction({"type": "view_submission", "user": user, "team": team,
                                    "api_app_id": run.phone_number_id, "trigger_id": trigger,
                                    "view": submitted})
        if 200 <= status < 300:
            await store.set_status(sf, run.id, opened.wamid, "closed")
        return {"delivered": 200 <= status < 300, "channels_status": status}

    if body.action:
        tapped = await store.message(sf, run.id, str(body.action.get("ts") or ""))
        if tapped is None or tapped.direction != "out":
            raise HTTPException(status_code=404, detail="No such message to click")
        for block in tapped.body.get("blocks") or []:
            for element in block.get("elements") or [] if block.get("type") == "actions" else []:
                if element.get("action_id") != body.action.get("action_id"):
                    continue
                status = await interaction({
                    "type": "block_actions", "user": user, "team": team,
                    "api_app_id": run.phone_number_id, "trigger_id": trigger,
                    "container": {"type": "message", "message_ts": tapped.wamid,
                                  "channel_id": slack.dm_of(person.address), "is_ephemeral": False},
                    "channel": {"id": slack.dm_of(person.address), "name": "directmessage"},
                    "message": tapped.body,
                    "actions": [{"type": "button", "action_id": element.get("action_id"),
                                 "block_id": block.get("block_id", ""),
                                 "value": element.get("value", ""), "text": element.get("text"),
                                 "action_ts": slack.new_ts()}],
                })
                return {"delivered": 200 <= status < 300, "channels_status": status}
        raise HTTPException(status_code=404, detail="No such button")

    if not (body.text and body.text.strip()):
        raise HTTPException(status_code=422, detail="Write something, or click a button")
    ts = slack.new_ts()
    event = {"type": "message", "channel": slack.dm_of(person.address), "channel_type": "im",
             "user": person.address, "text": body.text, "ts": ts, "event_ts": ts, "team": team["id"]}
    await store.log_message(sf, run_id=run.id, person_id=person.id, direction="in", kind="message",
                            body=event, wamid=ts)
    raw = json.dumps({
        "token": "simulation", "team_id": team["id"], "api_app_id": run.phone_number_id,
        "event": event, "type": "event_callback", "event_id": f"Ev{secrets.token_hex(6)}",
        "event_time": int(datetime.now(timezone.utc).timestamp()),
        "authorizations": [{"team_id": team["id"], "user_id": slack.bot_user_id(run), "is_bot": True}],
    }).encode()
    status = await deliver(path="", raw=raw, content_type="application/json")
    delivered = 200 <= status < 300
    await store.set_status(sf, run.id, ts, "sent" if delivered else "failed")
    return {"delivered": delivered, "channels_status": status}


@router.post("/runs/{run_id}/people/{person_id}/call")
async def call_as_person(run_id: uuid.UUID, person_id: uuid.UUID, org_id: uuid.UUID, request: Request):
    """A LiveKit token that phones the run's number as its person."""
    state = request.app.state
    run = await _run_in_org(request, run_id, org_id)
    if run.channel != "voice":
        raise HTTPException(status_code=422, detail="Only a voice simulation places calls")
    if run.status != "running":
        raise HTTPException(status_code=409, detail="This simulation has ended")
    person = next((p for p in await store.people(state.session_factory, run.id) if p.id == person_id), None)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person in this simulation")
    if not state.settings.voice.livekit_api_key:
        raise HTTPException(status_code=503, detail="Phone calls are not set up on this deployment")
    return voice.call_token(state.settings, run, person)


@router.post("/runs/{run_id}/people/{person_id}/next-step")
async def next_step(run_id: uuid.UUID, person_id: uuid.UUID, org_id: uuid.UUID, request: Request):
    """The person's next program check-in, sent now rather than when it falls due.

    The only way a check-in reaches a simulated person: the scheduled ticker
    leaves them out.  Each call moves on past the step it sent, as time would.
    """
    from surogates.followup import store as followup_store
    from surogates.followup.context import in_person_zone
    from surogates.followup.eligibility import next_contact
    from surogates.programs.materialize import (
        already_reached,
        identity_lookup_from,
        last_inbound_for,
        materialize_occurrence,
    )

    state = request.app.state
    sf = state.session_factory
    run = await _run_in_org(request, run_id, org_id)
    person = await _running_person(state, run, person_id)
    enrolled = person.user_id and await followup_store.enrollments(
        sf, org_id=run.org_id, agent_id=run.agent_id, user_id=person.user_id, status="active",
    )
    if not enrolled:
        raise HTTPException(status_code=409, detail="Not enrolled in a program")
    now = datetime.now(timezone.utc)
    after = max(now, person.next_after + timedelta(seconds=1)) if person.next_after else now
    lookup = identity_lookup_from(sf)
    async with sf() as db:
        schedules = (await db.execute(
            sa.select(ProgramScheduleRow)
            .where(ProgramScheduleRow.org_id == run.org_id)
            .where(ProgramScheduleRow.agent_id == run.agent_id)
            .where(ProgramScheduleRow.active.is_(True))
        )).scalars().all()
    due = []
    for schedule in schedules:
        last = await last_inbound_for(schedule, person.user_id, session_factory=sf,
                                      identity_lookup=lookup)
        for e in enrolled:
            found = next_contact(
                in_person_zone([schedule.config or {}], e), protocol=e.protocol,
                start_date=e.start_date, phase=e.phase, enrolled_at=e.created_at,
                last_inbound_at=last, now=after,
            )
            if found:
                due.append((found[0], schedule))
    if not due:
        raise HTTPException(status_code=409, detail="No program step is ahead for this person")
    at, schedule = min(due, key=lambda d: d[0])
    # Recorded now, not at the due slot: that slot's occurrence belongs to
    # everyone else on the roster, and taking it would skip them.
    occurrence_id = await materialize_occurrence(
        schedule, session_factory=sf, identity_lookup=lookup,
        now=at, only=person.user_id, run=run, sent_at=now,
    )
    await store.update_person(sf, person.id, next_after=at)
    async with sf() as db:
        row = (await db.execute(
            sa.select(ProgramInvitationRow)
            .where(ProgramInvitationRow.occurrence_id == occurrence_id)
            .where(ProgramInvitationRow.user_id == person.user_id)
        )).scalars().first()
    sent = row is not None and row.delivery_state == "queued"
    if sent:
        reason = None
    elif row is not None:
        reason = row.reason
    elif (schedule.config or {}).get("once_per_person") and person.user_id in await already_reached(
        sf, schedule.program_id, [person.user_id],
    ):
        reason = "Already reached in this program"
    else:
        reason = "Not due at that step"
    return {"sent": sent, "due_at": at.isoformat(), "reason": reason}


@router.post("/runs/{run_id}/end")
async def end_run(run_id: uuid.UUID, org_id: uuid.UUID, request: Request):
    await _run_in_org(request, run_id, org_id)
    state = request.app.state
    run = await store.finish_run(state, run_id)
    return await _detail(state.session_factory, run)
