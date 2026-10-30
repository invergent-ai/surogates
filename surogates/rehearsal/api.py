"""The internal API ops calls to start runs and write as their people.

Only ops calls it, with ``rehearsal.internal_token``; ops checks the Studio
user's rights on the agent first and passes the org every call is scoped to.
"""

from __future__ import annotations

import functools
import json
import logging

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from surogates.rehearsal import store
from surogates.rehearsal import discord, slack, teams, voice
from surogates.rehearsal.webhook import (
    post_slack,
    post_teams_activity,
    post_telegram_update,
    post_webhook,
)


def _require_internal_token(request: Request) -> None:
    expected = request.app.state.settings.rehearsal.internal_token
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="Rehearsal internal token required")


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", dependencies=[Depends(_require_internal_token)])


class StartRun(BaseModel):
    org_id: uuid.UUID
    agent_id: str
    created_by: str
    person_name: str = Field(min_length=1, max_length=80)
    routing_config: dict[str, Any] = Field(default_factory=dict)
    templates: list[dict[str, Any]] = Field(default_factory=list)
    flows: list[dict[str, Any]] = Field(default_factory=list)
    channel: Literal["whatsapp", "telegram", "slack", "teams", "discord", "website", "voice"] = "whatsapp"


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
            "channel": run.channel,
            "phone_number_id": run.phone_number_id, "created_by": run.created_by,
            "created_at": run.created_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None, "people": people,
            "flows": run.flows or []}


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
        templates=body.templates, flows=body.flows, channel=body.channel,
    )
    return await _detail(state.session_factory, run)


@router.get("/runs")
async def list_runs(org_id: uuid.UUID, agent_id: str, request: Request):
    sf = request.app.state.session_factory
    return [{"id": str(r.id), "status": r.status, "channel": r.channel,
             "created_by": r.created_by,
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
    if run.channel == "website":
        raise HTTPException(status_code=422, detail="On a website the visitor writes in the widget")
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
        state.http, channels_url=state.settings.rehearsal.channels_url,
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
            state.http, channels_url=state.settings.rehearsal.channels_url,
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
        state.http, channels_url=state.settings.rehearsal.channels_url,
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
        state.http, channels_url=state.settings.rehearsal.channels_url,
        app_id=run.phone_number_id, token=teams.sign(run.phone_number_id, service),
        activity=activity,
    )
    delivered = 200 <= status < 300
    await store.set_status(sf, run.id, activity_id, "sent" if delivered else "failed")
    return {"delivered": delivered, "channels_status": status}


async def _write_on_slack(state, run, person, body: PersonMessage) -> dict[str, Any]:
    """The member's message, button click or modal, as Slack would deliver it."""
    sf = state.session_factory
    team = {"id": slack.team_id(run), "domain": "rehearsal"}
    user = {"id": person.address, "username": person.name.lower(), "name": person.name.lower(),
            "team_id": team["id"]}
    deliver = functools.partial(
        post_slack, state.http, channels_url=state.settings.rehearsal.channels_url,
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
        "token": "rehearsal", "team_id": team["id"], "api_app_id": run.phone_number_id,
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
        raise HTTPException(status_code=422, detail="Only a voice rehearsal places calls")
    if run.status != "running":
        raise HTTPException(status_code=409, detail="This rehearsal has ended")
    person = next((p for p in await store.people(state.session_factory, run.id) if p.id == person_id), None)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person in this rehearsal")
    if not state.settings.voice.livekit_api_key:
        raise HTTPException(status_code=503, detail="Phone calls are not set up on this deployment")
    return voice.call_token(state.settings, run, person)


@router.post("/runs/{run_id}/end")
async def end_run(run_id: uuid.UUID, org_id: uuid.UUID, request: Request):
    await _run_in_org(request, run_id, org_id)
    state = request.app.state
    run = await store.end_run(state.session_factory, state.vault, run_id)
    if run.channel == "discord":
        await discord.GATEWAYS.close(run.id)
    if run.channel == "voice" and state.settings.voice.livekit_api_key:
        try:
            await voice.hang_up_all(state.settings, run)
        except Exception:  # noqa: BLE001 - the run is ended; a call left ringing ends at its own limit
            logger.warning("could not hang up the calls of rehearsal %s", run.id, exc_info=True)
    return await _detail(state.session_factory, run)
