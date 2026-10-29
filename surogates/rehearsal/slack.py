"""A rehearsal world's Slack Web API, called by the runtime's real Slack adapter.

Mounted at ``/s/api/{method}``: the adapter's SDK client for a rehearsal bot
token posts here, so what it posts, edits, deletes and the modals it opens
land in the run that owns the app.  The workspace has two members: the
person, and the agent's bot.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request

from surogates.rehearsal import store

router = APIRouter(prefix="/s")


def team_id(run) -> str:
    return f"T{run.phone_number_id}"


def bot_user_id(run) -> str:
    return f"B{run.phone_number_id}"


def dm_of(address: str) -> str:
    """A member's DM with the bot: ``U999…`` talks in ``D999…``."""
    return "D" + address[1:]


def new_ts() -> str:
    # Slack ts values are unique per channel and sort by time.
    return f"{time.time():.6f}"


def _error(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error}


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) and value[:1] in "[{" else value


async def _args(request: Request) -> dict[str, Any]:
    """The SDK mixes query strings, forms and JSON bodies."""
    args: dict[str, Any] = dict(request.query_params)
    raw = await request.body()
    if raw:
        if "json" in request.headers.get("content-type", ""):
            args.update(json.loads(raw))
        else:
            args.update(dict(await request.form()))
    return args


def user_object(run, person) -> dict[str, Any]:
    if person is None:
        return {"id": bot_user_id(run), "team_id": team_id(run), "name": "agent",
                "real_name": "Agent", "is_bot": True, "deleted": False, "profile": {}}
    return {"id": person.address, "team_id": team_id(run), "name": person.name.lower(),
            "real_name": person.name, "is_bot": False, "deleted": False, "tz": "Europe/Bucharest",
            "profile": {"display_name": person.name, "real_name": person.name}}


async def _member(sf, run, channel: str):
    """The person whose DM *channel* is."""
    for person in await store.people(sf, run.id):
        if channel in (dm_of(person.address), person.address):
            return person
    return None


@router.api_route("/api/{method}", methods=["GET", "POST"])
async def call(method: str, request: Request):
    args = await _args(request)
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or args.get("token", "")
    sf = request.app.state.session_factory
    run = await store.run_by_token(sf, token) if token else None
    if run is None or run.channel != "slack" or run.status != "running":
        return _error("invalid_auth" if token else "not_authed")

    if method == "auth.test":
        return {"ok": True, "url": "https://rehearsal.slack.com/", "team": "Rehearsal",
                "user": "agent", "team_id": team_id(run), "user_id": bot_user_id(run),
                "bot_id": bot_user_id(run), "app_id": run.phone_number_id,
                "is_enterprise_install": False}

    if method in ("conversations.history", "conversations.replies"):
        return {"ok": True, "messages": [], "has_more": False,
                "response_metadata": {"next_cursor": ""}}
    if method == "conversations.list":
        return {"ok": True, "channels": [], "response_metadata": {"next_cursor": ""}}

    if method == "users.info":
        uid = str(args.get("user") or "")
        if uid == bot_user_id(run):
            return {"ok": True, "user": user_object(run, None)}
        person = next((p for p in await store.people(sf, run.id) if p.address == uid), None)
        return {"ok": True, "user": user_object(run, person)} if person else _error("user_not_found")

    if method == "users.list":
        members = [user_object(run, p) for p in await store.people(sf, run.id)]
        return {"ok": True, "members": [*members, user_object(run, None)],
                "response_metadata": {"next_cursor": ""}}

    if method == "conversations.open":
        uid = str(args.get("users") or "").split(",")[0]
        person = next((p for p in await store.people(sf, run.id) if p.address == uid), None)
        return {"ok": True, "channel": {"id": dm_of(uid)}} if person else _error("user_not_found")

    if method == "conversations.info":
        person = await _member(sf, run, str(args.get("channel") or ""))
        if person is None:
            return _error("channel_not_found")
        return {"ok": True, "channel": {"id": dm_of(person.address), "is_im": True,
                                        "user": person.address, "created": 0}}

    if method == "chat.postMessage":
        person = await _member(sf, run, str(args.get("channel") or ""))
        if person is None:
            return _error("channel_not_found")
        ts = new_ts()
        message = {"type": "message", "user": bot_user_id(run), "bot_id": bot_user_id(run),
                   "text": args.get("text") or "", "blocks": _load(args.get("blocks")) or [],
                   "ts": ts, "channel": dm_of(person.address)}
        if args.get("thread_ts"):
            message["thread_ts"] = args["thread_ts"]
        await store.log_message(sf, run_id=run.id, person_id=person.id, direction="out",
                                kind="message", body=message, wamid=ts, status="sent")
        return {"ok": True, "channel": message["channel"], "ts": ts, "message": message}

    if method == "chat.update":
        ts = str(args.get("ts") or "")
        sent = await store.message(sf, run.id, ts)
        if sent is None or sent.direction != "out":
            return _error("message_not_found")
        edited = {**sent.body, "text": args.get("text", sent.body.get("text", "")),
                  "blocks": _load(args.get("blocks")) or [], "edited": {"ts": new_ts()}}
        await store.replace_body(sf, run.id, ts, edited)
        return {"ok": True, "channel": edited.get("channel"), "ts": ts, "text": edited["text"]}

    if method == "chat.delete":
        ts = str(args.get("ts") or "")
        if not await store.delete_message(sf, run.id, ts):
            return _error("message_not_found")
        return {"ok": True, "channel": args.get("channel"), "ts": ts}

    if method == "views.open":
        person = next((p for p in await store.people(sf, run.id)
                       if str(args.get("trigger_id") or "").startswith(p.address)), None)
        if person is None:
            return _error("invalid_trigger_id")
        view = {**(_load(args.get("view")) or {}), "id": f"V{secrets.token_hex(5).upper()}"}
        await store.log_message(sf, run_id=run.id, person_id=person.id, direction="out",
                                kind="view", body={"view": view}, wamid=view["id"], status="open")
        return {"ok": True, "view": view}

    return _error("unknown_method")
