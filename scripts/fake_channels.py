"""A fake Slack Web API and Telegram Bot API, with a browser UI for each person.

Point the runtime and ops at it:

    SUROGATES_SLACK_API_BASE=http://127.0.0.1:58124/slack/api/
    SUROGATES_TELEGRAM_API_BASE=http://127.0.0.1:58124/telegram

then connect the channels on the agent with

    Slack     app id A0FAKEPRETO, bot token xoxb-fake-pretorian, the signing secret given below
    Telegram  bot token 7000000001:AAFakeLocalStackTelegramToken

and open http://127.0.0.1:58124/slack and http://127.0.0.1:58124/telegram to write
as a workspace member or a Telegram user.

Slack never registers its webhook by API: the Events and Interactivity URLs are
typed into the Slack app, so the fake posts to ``--slack-events-base`` +
``/slack/{app_id}`` (and ``/interact``), signed with ``--slack-signing-secret``
exactly like Slack.  Telegram learns its webhook from ``setWebhook`` and sends
the ``secret_token`` back as ``X-Telegram-Bot-Api-Secret-Token``.  A Telegram
bot may not write first to someone who never started it, and the fake refuses
that the same way.  Files live in memory; chats, members and the webhook live in
``--state``.

    .venv/bin/python scripts/fake_channels.py --slack-signing-secret 0123456789abcdef0123456789abcdef
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import itertools
import json
import mimetypes
import os
import pathlib
import time
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

SLACK_APP_ID = "A0FAKEPRETO"
SLACK_TEAM_ID = "T0FAKEPRETO"
SLACK_BOT_USER = "U0BOTPRETO"
SLACK_BOT_ID = "B0BOTPRETO"
TG_BOT_ID = 7000000001

app = FastAPI()
_ids = itertools.count(1)
state: dict = {
    "rev": 0,
    "slack": {
        "team": "Clinica Pretorian",
        "events_base": "http://127.0.0.1:8003",
        "members": {
            "U0DRRADU01": {"name": "radu.pretorian", "real_name": "Dr. Radu Pretorian", "email": "radu@pretorian.local"},
            "U0ASMARIA1": {"name": "maria.asistent", "real_name": "Maria Ionescu (asistentă)", "email": "maria@pretorian.local"},
        },
        "modals": {},
    },
    "telegram": {
        "bot": {"id": TG_BOT_ID, "is_bot": True, "first_name": "Dr. Pretorian", "username": "pretorian_fake_bot"},
        "webhook": {},
        "users": {
            "7100000001": {"first_name": "Ion", "last_name": "Pacient", "username": "ion_pacient"},
            "7100000101": {"first_name": "Maria", "last_name": "Popescu", "username": "maria_popescu"},
        },
        "toasts": [],
    },
    "messages": [],
}
SLACK_SIGNING_SECRET = ""
STATE_FILE = ""
FIXTURES = ""
FILES: dict[str, tuple[bytes, str, str]] = {}


def _persist() -> None:
    state["rev"] += 1
    if STATE_FILE:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, ensure_ascii=False)


def _log(platform: str, direction: str, peer: str, **fields) -> dict:
    entry = {"platform": platform, "dir": direction, "peer": peer, "ts": time.time(), **fields}
    state["messages"].append(entry)
    _persist()
    return entry


async def _args(request: Request) -> dict:
    """Bot API and Web API callers mix query strings, forms and JSON bodies."""
    args = dict(request.query_params)
    raw = await request.body()
    if raw:
        if "json" in request.headers.get("content-type", ""):
            args.update(json.loads(raw))
        else:
            args.update(dict(await request.form()))
    return args


def _load(value):
    return json.loads(value) if isinstance(value, str) and value[:1] in "[{" else value


def _file_from(b: dict) -> tuple[str, bytes, str] | None:
    """A person's attachment from the script/UI API: a fixture name, a path, or base64."""
    if b.get("media_b64"):
        name, raw = b.get("filename") or "upload.bin", base64.b64decode(b["media_b64"])
    elif b.get("fixture"):
        path = pathlib.Path(FIXTURES, pathlib.Path(b["fixture"]).name)
        name, raw = path.name, path.read_bytes()
    elif b.get("media_path"):
        path = pathlib.Path(b["media_path"])
        name, raw = path.name, path.read_bytes()
    else:
        return None
    return name, raw, b.get("mime") or mimetypes.guess_type(name)[0] or "application/octet-stream"


# ---------------------------------------------------------------------------
# Slack Web API
# ---------------------------------------------------------------------------


def _dm(member: str) -> str:
    return "D" + member[1:]


def _member_of(channel: str) -> str | None:
    member = "U" + channel[1:] if channel.startswith("D") else channel
    return member if member in state["slack"]["members"] else None


def _slack_ts() -> str:
    # Slack ts values are unique per channel and sort by time.
    return f"{time.time():.6f}"[:-3] + f"{next(_ids) % 1000:03d}"


def _slack_user(uid: str) -> dict:
    if uid == SLACK_BOT_USER:
        return {"id": uid, "team_id": SLACK_TEAM_ID, "name": "pretorian", "real_name": "Dr. Pretorian (agent)",
                "is_bot": True, "deleted": False, "profile": {"display_name": "Dr. Pretorian", "real_name": "Dr. Pretorian (agent)"}}
    m = state["slack"]["members"][uid]
    return {"id": uid, "team_id": SLACK_TEAM_ID, "name": m["name"], "real_name": m["real_name"], "is_bot": False,
            "deleted": False, "tz": "Europe/Bucharest",
            "profile": {"display_name": m.get("display_name") or m["real_name"], "real_name": m["real_name"],
                        "email": m.get("email", "")}}


def _slack_file(fid: str, base: str, channel: str = "") -> dict:
    raw, mime, name = FILES[fid]
    url = f"{base}slack/files/{fid}/{name}"
    return {"id": fid, "name": name, "title": name, "mimetype": mime, "size": len(raw), "created": int(time.time()),
            "url_private": url, "url_private_download": url, "ims": [channel] if channel else []}


def _slack_find(channel: str, ts: str) -> dict | None:
    return next((m for m in state["messages"] if m["platform"] == "slack" and m.get("channel") == channel
                 and m.get("id") == ts), None)


def _slack_error(error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error})


async def _slack_post(args: dict, request: Request):
    member = _member_of(args.get("channel", ""))
    if member is None:
        return _slack_error("channel_not_found")
    ts = _slack_ts()
    entry = _log("slack", "out", member, id=ts, channel=_dm(member), text=args.get("text", ""),
                 blocks=_load(args.get("blocks")) or [], thread_ts=args.get("thread_ts"))
    return {"ok": True, "channel": entry["channel"], "ts": ts,
            "message": {"type": "message", "user": SLACK_BOT_USER, "bot_id": SLACK_BOT_ID, "text": entry["text"], "ts": ts}}


async def _slack_update(args: dict, request: Request):
    m = _slack_find(args.get("channel", ""), args.get("ts", ""))
    if m is None:
        return _slack_error("message_not_found")
    m.update(text=args.get("text", m["text"]), blocks=_load(args.get("blocks")) or [], edited=True)
    _persist()
    return {"ok": True, "channel": m["channel"], "ts": m["id"], "text": m["text"]}


async def _slack_delete(args: dict, request: Request):
    m = _slack_find(args.get("channel", ""), args.get("ts", ""))
    if m is None:
        return _slack_error("message_not_found")
    state["messages"].remove(m)
    _persist()
    return {"ok": True, "channel": m["channel"], "ts": m["id"]}


async def _slack_open(args: dict, request: Request):
    member = (args.get("users") or "").split(",")[0]
    if member not in state["slack"]["members"]:
        return _slack_error("user_not_found")
    return {"ok": True, "channel": {"id": _dm(member)}}


async def _slack_info(args: dict, request: Request):
    member = _member_of(args.get("channel", ""))
    if member is None:
        return _slack_error("channel_not_found")
    return {"ok": True, "channel": {"id": _dm(member), "is_im": True, "user": member, "created": 0}}


async def _slack_users_info(args: dict, request: Request):
    uid = args.get("user", "")
    if uid != SLACK_BOT_USER and uid not in state["slack"]["members"]:
        return _slack_error("user_not_found")
    return {"ok": True, "user": _slack_user(uid)}


async def _slack_users_list(args: dict, request: Request):
    members = [_slack_user(u) for u in state["slack"]["members"]] + [_slack_user(SLACK_BOT_USER)]
    members.append({"id": "USLACKBOT", "name": "slackbot", "real_name": "Slackbot", "is_bot": False, "deleted": False, "profile": {}})
    return {"ok": True, "members": members, "response_metadata": {"next_cursor": ""}}


async def _slack_views_open(args: dict, request: Request):
    trigger = args.get("trigger_id", "")
    member = trigger.split(".")[0]
    if member not in state["slack"]["members"]:
        return _slack_error("invalid_trigger_id")
    view = {**_load(args.get("view")), "id": f"V{next(_ids):08d}"}
    state["slack"]["modals"][member] = view
    _persist()
    return {"ok": True, "view": view}


async def _slack_upload_url(args: dict, request: Request):
    fid = f"F{next(_ids):08d}"
    FILES[fid] = (b"", "application/octet-stream", args.get("filename") or "file")
    return {"ok": True, "upload_url": f"{request.base_url}slack/upload/{fid}", "file_id": fid}


async def _slack_complete_upload(args: dict, request: Request):
    member = _member_of(args.get("channel_id", ""))
    files = _load(args.get("files")) or []
    if member is None:
        return {"ok": True, "files": [{"id": f["id"], "title": f.get("title")} for f in files]}
    base = str(request.base_url)
    entry = _log("slack", "out", member, id=_slack_ts(), channel=_dm(member), text=args.get("initial_comment", ""),
                 blocks=[], thread_ts=args.get("thread_ts"), files=[_slack_file(f["id"], base, _dm(member)) for f in files])
    return {"ok": True, "files": [{"id": f["id"], "title": f.get("title")} for f in entry["files"]]}


async def _slack_files_info(args: dict, request: Request):
    fid = args.get("file", "")
    if fid not in FILES:
        return _slack_error("file_not_found")
    holder = next((m for m in state["messages"] if m["platform"] == "slack"
                   and any(f["id"] == fid for f in m.get("files") or [])), None)
    return {"ok": True, "file": _slack_file(fid, str(request.base_url), (holder or {}).get("channel", ""))}


async def _slack_files_list(args: dict, request: Request):
    channel = args.get("channel", "")
    files = [f for m in state["messages"] if m["platform"] == "slack" and m.get("channel") == channel
             for f in m.get("files") or [] if f["id"] in FILES]
    return {"ok": True, "files": files, "paging": {"count": len(files), "total": len(files), "page": 1, "pages": 1}}


async def _slack_empty_history(args: dict, request: Request):
    return {"ok": True, "messages": [], "has_more": False, "response_metadata": {"next_cursor": ""}}


async def _slack_empty_list(args: dict, request: Request):
    return {"ok": True, "channels": [], "response_metadata": {"next_cursor": ""}}


async def _slack_auth_test(args: dict, request: Request):
    return {"ok": True, "url": "https://clinica-pretorian.slack.com/", "team": state["slack"]["team"],
            "user": "pretorian", "team_id": SLACK_TEAM_ID, "user_id": SLACK_BOT_USER, "bot_id": SLACK_BOT_ID,
            "app_id": SLACK_APP_ID, "is_enterprise_install": False}


SLACK_METHODS = {
    "auth.test": _slack_auth_test,
    "chat.postMessage": _slack_post,
    "chat.update": _slack_update,
    "chat.delete": _slack_delete,
    "conversations.open": _slack_open,
    "conversations.info": _slack_info,
    "conversations.history": _slack_empty_history,
    "conversations.list": _slack_empty_list,
    "users.info": _slack_users_info,
    "users.list": _slack_users_list,
    "views.open": _slack_views_open,
    "files.getUploadURLExternal": _slack_upload_url,
    "files.completeUploadExternal": _slack_complete_upload,
    "files.info": _slack_files_info,
    "files.list": _slack_files_list,
}


@app.api_route("/slack/api/{method}", methods=["GET", "POST"])
async def slack_api(method: str, request: Request):
    args = await _args(request)
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or args.get("token", "")
    if not token.startswith("xox"):
        return _slack_error("invalid_auth" if token else "not_authed")
    handler = SLACK_METHODS.get(method)
    if handler is None:
        return _slack_error("unknown_method")
    return await handler(args, request)


@app.post("/slack/upload/{fid}")
async def slack_upload(fid: str, request: Request):
    _, _, name = FILES[fid]
    raw = await request.body()
    FILES[fid] = (raw, mimetypes.guess_type(name)[0] or "application/octet-stream", name)
    return Response(f"OK - {len(raw)}", media_type="text/plain")


@app.get("/slack/files/{fid}/{name}")
async def slack_file(fid: str, name: str, request: Request):
    if not request.headers.get("authorization", "").startswith("Bearer xox"):
        return Response(status_code=302, headers={"Location": "https://slack.com/signin"})
    if fid not in FILES:
        return Response(status_code=404)
    raw, mime, _ = FILES[fid]
    return Response(raw, media_type=mime)


@app.get("/slack/view/{fid}/{name}")
async def slack_file_view(fid: str, name: str):
    """The same file for the browser UI, which has no bot token."""
    if fid not in FILES:
        return Response(status_code=404)
    raw, mime, _ = FILES[fid]
    return Response(raw, media_type=mime)


async def _post(url: str, raw: bytes, headers: dict) -> httpx.Response:
    """Every webhook to the runtime goes through here, as the provider would send it."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            return await client.post(url, content=raw, headers=headers)
    except httpx.HTTPError as exc:
        # The runtime being down must not lose what the person typed.
        return httpx.Response(502, text=f"{type(exc).__name__}: {exc}")


def slack_signature(secret: str, ts: str, raw: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256).hexdigest()


async def _slack_deliver(path: str, raw: bytes, content_type: str) -> httpx.Response:
    ts = str(int(time.time()))
    return await _post(f"{state['slack']['events_base'].rstrip('/')}/slack/{SLACK_APP_ID}{path}", raw, {
        "Content-Type": content_type,
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": slack_signature(SLACK_SIGNING_SECRET, ts, raw),
    })


def slack_event_body(member: str, text: str, ts: str, files: list[dict] | None = None) -> dict:
    event = {"type": "message", "channel": _dm(member), "channel_type": "im", "user": member, "text": text,
             "ts": ts, "event_ts": ts, "team": SLACK_TEAM_ID}
    if files:
        event |= {"subtype": "file_share", "files": files}
    return {"token": "fake-verification-token", "team_id": SLACK_TEAM_ID, "api_app_id": SLACK_APP_ID,
            "event": event, "type": "event_callback", "event_id": f"Ev{next(_ids):08d}", "event_time": int(time.time()),
            "authorizations": [{"team_id": SLACK_TEAM_ID, "user_id": SLACK_BOT_USER, "is_bot": True}]}


def _slack_payload_user(member: str) -> dict:
    m = state["slack"]["members"][member]
    return {"id": member, "username": m["name"], "name": m["name"], "team_id": SLACK_TEAM_ID}


@app.post("/api/slack/send")
async def slack_send(request: Request):
    """The member writes in their DM with the bot: text, or a file with an optional comment."""
    b = await request.json()
    member, ts = b["member"], _slack_ts()
    files = []
    upload = _file_from(b)
    if upload:
        name, raw, mime = upload
        fid = f"F{next(_ids):08d}"
        FILES[fid] = (raw, mime, name)
        files = [_slack_file(fid, str(request.base_url), _dm(member))]
    text = b.get("text") or b.get("caption") or ""
    _log("slack", "in", member, id=ts, channel=_dm(member), text=text, files=files)
    r = await _slack_deliver("", json.dumps(slack_event_body(member, text, ts, files)).encode(), "application/json")
    return {"ok": r.is_success, "status": r.status_code, "ts": ts}


@app.post("/api/slack/tap")
async def slack_tap(request: Request):
    """Click a Block Kit button, by its label (latest message that has it) or by action_id."""
    b = await request.json()
    member = b["member"]
    for m in reversed(state["messages"]):
        if m["platform"] != "slack" or m["peer"] != member or m["dir"] != "out" or (b.get("ts") and m["id"] != b["ts"]):
            continue
        for block in m.get("blocks") or []:
            for el in block.get("elements") or [] if block.get("type") == "actions" else []:
                label = ((el.get("text") or {}).get("text") or "")
                if el.get("type") == "button" and (label == b.get("label") or el.get("action_id") == b.get("action_id")):
                    payload = {
                        "type": "block_actions", "user": _slack_payload_user(member), "api_app_id": SLACK_APP_ID,
                        "token": "fake-verification-token", "team": {"id": SLACK_TEAM_ID, "domain": "clinica-pretorian"},
                        "container": {"type": "message", "message_ts": m["id"], "channel_id": m["channel"], "is_ephemeral": False},
                        "trigger_id": f"{member}.{next(_ids)}.{int(time.time())}",
                        "channel": {"id": m["channel"], "name": "directmessage"},
                        "message": {"type": "message", "user": SLACK_BOT_USER, "bot_id": SLACK_BOT_ID, "ts": m["id"],
                                    "text": m["text"], "blocks": m["blocks"]},
                        "actions": [{"type": "button", "action_id": el.get("action_id"), "block_id": block.get("block_id", ""),
                                     "value": el.get("value", ""), "text": el.get("text"), "action_ts": f"{time.time():.6f}"}],
                    }
                    state["slack"]["modals"].pop(member, None)
                    r = await _slack_deliver("/interact", urlencode({"payload": json.dumps(payload)}).encode(),
                                             "application/x-www-form-urlencoded")
                    return {"ok": r.is_success, "status": r.status_code, "modal": state["slack"]["modals"].get(member)}
    return JSONResponse({"ok": False, "error": "no such button"}, status_code=404)


def modal_state(view: dict, answers: dict) -> dict:
    """Slack's ``view.state.values`` for *answers*, keyed by block_id: an option label or value, or text."""
    values: dict = {}
    for block in view.get("blocks") or []:
        el = block.get("element") or {}
        answer = answers.get(block.get("block_id"))
        if block.get("type") != "input" or answer in (None, ""):
            if el.get("type") == "plain_text_input":
                values[block["block_id"]] = {el["action_id"]: {"type": "plain_text_input", "value": None}}
            continue
        if el.get("type") == "static_select":
            opt = next((o for o in el.get("options") or []
                        if answer in (o["value"], (o.get("text") or {}).get("text"))), None)
            values[block["block_id"]] = {el["action_id"]: {"type": "static_select", "selected_option": opt}}
        else:
            values[block["block_id"]] = {el["action_id"]: {"type": "plain_text_input", "value": str(answer)}}
    return values


@app.post("/api/slack/modal")
async def slack_modal(request: Request):
    """Submit (or cancel) the modal the bot opened for this member."""
    b = await request.json()
    member = b["member"]
    view = state["slack"]["modals"].get(member)
    if view is None:
        return JSONResponse({"ok": False, "error": "no open modal"}, status_code=404)
    if b.get("cancel"):
        state["slack"]["modals"].pop(member)
        _persist()
        return {"ok": True}
    submitted = {**view, "state": {"values": modal_state(view, b.get("values") or {})}}
    payload = {"type": "view_submission", "user": _slack_payload_user(member), "api_app_id": SLACK_APP_ID,
               "token": "fake-verification-token", "team": {"id": SLACK_TEAM_ID, "domain": "clinica-pretorian"},
               "trigger_id": f"{member}.{next(_ids)}.{int(time.time())}", "view": submitted}
    r = await _slack_deliver("/interact", urlencode({"payload": json.dumps(payload)}).encode(),
                             "application/x-www-form-urlencoded")
    body = r.json() if r.content and "json" in r.headers.get("content-type", "") else {}
    if r.is_success and body.get("response_action") != "errors":
        state["slack"]["modals"].pop(member, None)
        _persist()
    return {"ok": r.is_success, "status": r.status_code, "response": body}


@app.post("/api/slack/members")
async def slack_member(request: Request):
    b = await request.json()
    state["slack"]["members"][b["id"]] = {"name": b.get("name") or b["id"].lower(),
                                         "real_name": b.get("real_name") or b["id"], "email": b.get("email", "")}
    _persist()
    return {"ok": True}


@app.post("/api/slack/config")
async def slack_config(request: Request):
    b = await request.json()
    if b.get("events_base"):
        state["slack"]["events_base"] = b["events_base"]
    if b.get("team"):
        state["slack"]["team"] = b["team"]
    _persist()
    return {"ok": True}


@app.get("/api/slack/thread")
async def slack_thread(member: str):
    msgs = [m for m in state["messages"] if m["platform"] == "slack" and m["peer"] == member]
    return {"messages": msgs, "modal": state["slack"]["modals"].get(member), "rev": state["rev"],
            "member": _slack_user(member) if member in state["slack"]["members"] else None}


# ---------------------------------------------------------------------------
# Telegram Bot API
# ---------------------------------------------------------------------------


def _tg_user(uid: str) -> dict:
    u = state["telegram"]["users"][uid]
    return {"id": int(uid), "is_bot": False, "first_name": u["first_name"], "last_name": u.get("last_name", ""),
            "username": u.get("username", ""), "language_code": "ro"}


def _tg_chat(uid: str) -> dict:
    u = state["telegram"]["users"][uid]
    return {"id": int(uid), "type": "private", "first_name": u["first_name"], "last_name": u.get("last_name", ""),
            "username": u.get("username", "")}


def _tg_message(entry: dict) -> dict:
    out = {"message_id": entry["id"], "date": int(entry["ts"]), "chat": _tg_chat(entry["peer"])}
    out["from"] = state["telegram"]["bot"] if entry["dir"] == "out" else _tg_user(entry["peer"])
    if entry.get("text"):
        out["text"] = entry["text"]
    if entry.get("reply_markup"):
        out["reply_markup"] = entry["reply_markup"]
    return out


def _tg_error(code: int, description: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error_code": code, "description": description}, status_code=code)


def _tg_find(chat_id, message_id) -> dict | None:
    return next((m for m in state["messages"] if m["platform"] == "telegram" and m["peer"] == str(chat_id)
                 and m["id"] == int(message_id)), None)


def _tg_started(uid: str) -> bool:
    return any(m["platform"] == "telegram" and m["peer"] == uid and m["dir"] == "in" for m in state["messages"])


def _tg_files_of(entry: dict) -> list[str]:
    return [f["file_id"] for f in entry.get("files") or []]


@app.api_route("/telegram/bot{token}/{method}", methods=["GET", "POST"])
async def telegram_api(token: str, method: str, request: Request):
    if not token.startswith(f"{TG_BOT_ID}:"):
        return _tg_error(401, "Unauthorized")
    a = await _args(request)
    tg = state["telegram"]
    if method == "getMe":
        return {"ok": True, "result": {**tg["bot"], "can_join_groups": True, "can_read_all_group_messages": False,
                                       "supports_inline_queries": False}}
    if method == "setWebhook":
        tg["webhook"] = {"url": a.get("url", ""), "secret_token": a.get("secret_token", ""),
                         "allowed_updates": _load(a.get("allowed_updates")) or []}
        _persist()
        return {"ok": True, "result": True, "description": "Webhook was set"}
    if method == "deleteWebhook":
        tg["webhook"] = {}
        _persist()
        return {"ok": True, "result": True, "description": "Webhook was deleted"}
    if method == "getWebhookInfo":
        return {"ok": True, "result": {"url": tg["webhook"].get("url", ""), "has_custom_certificate": False,
                                       "pending_update_count": 0}}
    chat = str(a.get("chat_id", ""))
    if method == "sendMessage":
        if chat not in tg["users"]:
            return _tg_error(400, "Bad Request: chat not found")
        if not _tg_started(chat):
            return _tg_error(403, "Forbidden: bot can't initiate conversation with a user")
        if not (a.get("text") or "").strip():
            return _tg_error(400, "Bad Request: message text is empty")
        reply_to = (_load(a.get("reply_parameters")) or {}).get("message_id")
        entry = _log("telegram", "out", chat, id=next(_ids), text=a["text"], parse_mode=a.get("parse_mode"),
                     reply_markup=_load(a.get("reply_markup")), reply_to=reply_to)
        return {"ok": True, "result": _tg_message(entry)}
    if method == "editMessageText":
        m = _tg_find(chat, a.get("message_id", 0))
        if m is None:
            return _tg_error(400, "Bad Request: message to edit not found")
        # Telegram drops the inline keyboard of an edited message unless the edit carries one.
        m.update(text=a.get("text", ""), parse_mode=a.get("parse_mode"), reply_markup=_load(a.get("reply_markup")), edited=True)
        _persist()
        return {"ok": True, "result": _tg_message(m)}
    if method == "answerCallbackQuery":
        tg["toasts"].append({"id": a.get("callback_query_id"), "text": a.get("text", ""), "ts": time.time()})
        del tg["toasts"][:-20]
        _persist()
        return {"ok": True, "result": True}
    if method == "setMessageReaction":
        m = _tg_find(chat, a.get("message_id", 0))
        if m is None:
            return _tg_error(400, "Bad Request: message to react not found")
        m["reactions"] = [r.get("emoji") for r in _load(a.get("reaction")) or []]
        _persist()
        return {"ok": True, "result": True}
    if method == "sendChatAction":
        tg.setdefault("typing", {})[chat] = time.time()
        return {"ok": True, "result": True}
    if method == "getFile":
        fid = a.get("file_id", "")
        if fid not in FILES:
            return _tg_error(400, "Bad Request: invalid file_id")
        raw, _, name = FILES[fid]
        return {"ok": True, "result": {"file_id": fid, "file_unique_id": fid[-8:], "file_size": len(raw),
                                       "file_path": f"documents/{fid}/{name}"}}
    return _tg_error(404, "Not Found")


@app.get("/telegram/file/bot{token}/documents/{fid}/{name}")
async def telegram_file(token: str, fid: str, name: str):
    if not token.startswith(f"{TG_BOT_ID}:") or fid not in FILES:
        return Response(status_code=404)
    raw, mime, _ = FILES[fid]
    return Response(raw, media_type=mime)


@app.get("/telegram/view/{fid}")
async def telegram_file_view(fid: str):
    if fid not in FILES:
        return Response(status_code=404)
    raw, mime, _ = FILES[fid]
    return Response(raw, media_type=mime)


async def _tg_deliver(update: dict) -> httpx.Response | None:
    hook = state["telegram"]["webhook"]
    if not hook.get("url"):
        return None
    headers = {"Content-Type": "application/json"}
    if hook.get("secret_token"):
        headers["X-Telegram-Bot-Api-Secret-Token"] = hook["secret_token"]
    return await _post(hook["url"], json.dumps(update).encode(), headers)


@app.post("/api/telegram/send")
async def telegram_send(request: Request):
    """The user writes to the bot: text (``/start`` included), or a photo, voice note or document."""
    b = await request.json()
    uid = str(b["user"])
    entry = _log("telegram", "in", uid, id=next(_ids), text=b.get("text") or "")
    message = _tg_message(entry)
    upload = _file_from(b)
    if upload:
        name, raw, mime = upload
        fid = f"AgACAgFake{next(_ids):08d}"
        FILES[fid] = (raw, mime, name)
        meta = {"file_id": fid, "file_unique_id": fid[-8:], "file_size": len(raw)}
        kind = b.get("kind") or ("photo" if mime.startswith("image/") else "voice" if mime.startswith("audio/") else "document")
        if kind == "photo":
            message["photo"] = [{**meta, "width": 1280, "height": 960}]
        elif kind == "voice":
            message["voice"] = {**meta, "duration": 5, "mime_type": mime}
        else:
            message["document"] = {**meta, "file_name": name, "mime_type": mime}
        message.pop("text", None)
        if b.get("caption") or b.get("text"):
            message["caption"] = b.get("caption") or b.get("text")
        entry.update(text=message.get("caption", ""), files=[{"file_id": fid, "name": name, "mime": mime, "kind": kind}])
        _persist()
    if (b.get("text") or "").startswith("/"):
        command = b["text"].split()[0]
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
    r = await _tg_deliver({"update_id": next(_ids), "message": message})
    return {"ok": bool(r and r.is_success), "status": r.status_code if r else None, "message_id": entry["id"],
            "webhook": bool(r)}


@app.post("/api/telegram/tap")
async def telegram_tap(request: Request):
    """Tap an inline-keyboard button, by its label (latest message that has it) or its callback_data."""
    b = await request.json()
    uid = str(b["user"])
    for m in reversed(state["messages"]):
        if m["platform"] != "telegram" or m["peer"] != uid or m["dir"] != "out" or (b.get("message_id") and m["id"] != b["message_id"]):
            continue
        for row in (m.get("reply_markup") or {}).get("inline_keyboard") or []:
            for button in row:
                if "callback_data" in button and (button["text"] == b.get("label") or button["callback_data"] == b.get("data")):
                    query_id = str(next(_ids) * 7919)
                    update = {"update_id": next(_ids), "callback_query": {
                        "id": query_id, "from": _tg_user(uid), "message": _tg_message(m),
                        "chat_instance": f"-{uid}", "data": button["callback_data"]}}
                    r = await _tg_deliver(update)
                    toast = next((t["text"] for t in state["telegram"]["toasts"] if t["id"] == query_id), None)
                    return {"ok": bool(r and r.is_success), "status": r.status_code if r else None, "toast": toast}
    return JSONResponse({"ok": False, "error": "no such button"}, status_code=404)


@app.post("/api/telegram/users")
async def telegram_user(request: Request):
    b = await request.json()
    state["telegram"]["users"][str(b["id"])] = {"first_name": b.get("first_name") or str(b["id"]),
                                               "last_name": b.get("last_name", ""), "username": b.get("username", "")}
    _persist()
    return {"ok": True}


@app.get("/api/telegram/thread")
async def telegram_thread(user: str):
    msgs = [m for m in state["messages"] if m["platform"] == "telegram" and m["peer"] == user]
    typing = time.time() - (state["telegram"].get("typing") or {}).get(user, 0) < 5
    toast = next((t for t in reversed(state["telegram"]["toasts"]) if time.time() - t["ts"] < 4 and t["text"]), None)
    return {"messages": msgs, "rev": state["rev"], "typing": typing, "toast": toast,
            "user": _tg_user(user) if user in state["telegram"]["users"] else None, "bot": state["telegram"]["bot"]}


@app.get("/api/state")
async def debug_state():
    tg = state["telegram"]
    return {"rev": state["rev"],
            "slack": {"team": state["slack"]["team"], "app_id": SLACK_APP_ID, "team_id": SLACK_TEAM_ID,
                      "bot_user_id": SLACK_BOT_USER, "events_base": state["slack"]["events_base"],
                      "members": state["slack"]["members"], "signing_secret_set": bool(SLACK_SIGNING_SECRET)},
            "telegram": {"bot": tg["bot"], "webhook_url": tg["webhook"].get("url", ""),
                         "webhook_secret_set": bool(tg["webhook"].get("secret_token")), "users": tg["users"]}}


@app.get("/", response_class=HTMLResponse)
async def index():
    return '<meta charset=utf-8><title>Fake channels</title><p><a href="/slack">Slack</a> · <a href="/telegram">Telegram</a> · <a href="/api/state">state</a>'


@app.get("/slack", response_class=HTMLResponse)
async def slack_page():
    return SLACK_HTML


@app.get("/telegram", response_class=HTMLResponse)
async def telegram_page():
    return TELEGRAM_HTML


_COMMON_JS = r"""
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hm=t=>new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
const api=(p,b)=>fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
function fileB64(f){return new Promise(res=>{const r=new FileReader();r.onload=()=>res(r.result.split(',')[1]);r.readAsDataURL(f)})}
function store(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){}}
"""

SLACK_HTML = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Fake Slack</title>
<style>
:root{--side:#3f0e40;--side2:#350d36;--sidetx:#cfc3cf;--act:#1164a3;--bg:#fff;--tx:#1d1c1d;--mut:#616061;--line:#ddd;--hov:#f8f8f8;--btn:#007a5a;--code:#e01e5a;--quote:#ddd}
@media (prefers-color-scheme:dark){:root{--side:#19171d;--side2:#121016;--sidetx:#ababad;--bg:#1a1d21;--tx:#d1d2d3;--mut:#9a9b9e;--line:#35373b;--hov:#222529;--code:#e8912d;--quote:#545454}}
*{box-sizing:border-box}body{margin:0;height:100vh;display:flex;font:15px/1.46 Lato,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--tx)}
#side{width:260px;flex:none;background:var(--side);color:var(--sidetx);display:flex;flex-direction:column}
#ws{padding:14px 16px;font-weight:900;font-size:18px;color:#fff;border-bottom:1px solid #ffffff1a}
#side h4{margin:16px 16px 6px;font-size:13px;font-weight:600;opacity:.8}
.dm{display:flex;align-items:center;gap:8px;padding:5px 16px;cursor:pointer;font-size:15px}.dm:hover{background:var(--side2)}.dm.on{background:var(--act);color:#fff}
.dm .dot{width:9px;height:9px;border-radius:50%;background:#2bac76}.dm .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.dm .badge{background:#cd2553;color:#fff;border-radius:9px;font-size:11px;padding:0 6px;font-weight:700}
#side form{margin:auto 12px 12px;display:flex;flex-direction:column;gap:4px;font-size:12px}#side input{background:var(--side2);border:1px solid #ffffff2a;color:#fff;border-radius:4px;padding:4px 6px}#side button{background:#ffffff1a;color:#fff;border:0;border-radius:4px;padding:5px;cursor:pointer}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#hd{padding:12px 20px;border-bottom:1px solid var(--line);font-weight:900;font-size:18px;display:flex;align-items:baseline;gap:10px}#hd small{font-weight:400;color:var(--mut);font-size:13px}
#list{flex:1;overflow-y:auto;padding:12px 0}
.msg{display:flex;gap:8px;padding:6px 20px}.msg:hover{background:var(--hov)}.msg.cont{padding-top:0}.msg.cont .av{visibility:hidden;height:0}
.av{width:36px;height:36px;border-radius:6px;flex:none;display:grid;place-items:center;color:#fff;font-weight:700;font-size:14px}
.body{min-width:0;flex:1}.who b{font-weight:900}.who span{color:var(--mut);font-size:12px;margin-left:6px}.who .app{background:#ddd3;color:var(--mut);font-size:10px;padding:0 3px;border-radius:2px;margin-left:4px;font-weight:700}
.txt{white-space:pre-wrap;overflow-wrap:anywhere}.txt code{color:var(--code);background:#8881;border:1px solid #8883;border-radius:3px;padding:0 3px;font-size:12px}.txt blockquote{margin:2px 0;border-left:4px solid var(--quote);padding-left:10px}
.edited{color:var(--mut);font-size:12px}.thinking{color:var(--mut);font-style:italic}
.blk{margin:4px 0}.acts{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}
.sbtn{border:1px solid #8886;background:var(--bg);color:var(--tx);border-radius:4px;padding:3px 12px;font-weight:700;font-size:14px;cursor:pointer;height:30px}.sbtn.primary{background:var(--btn);border-color:var(--btn);color:#fff}.sbtn.danger{background:#e01e5a;border-color:#e01e5a;color:#fff}
.file{display:inline-flex;align-items:center;gap:10px;border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-top:4px;color:var(--tx);text-decoration:none}.file img{max-width:320px;max-height:240px;border-radius:6px;display:block}
.thr{color:var(--act);font-size:12px}
#cmp{margin:0 20px 20px;border:1px solid #8888;border-radius:8px}#cmp textarea{width:100%;border:0;outline:0;resize:none;background:none;color:var(--tx);font:inherit;padding:10px 12px;min-height:44px}
#cmp .bar{display:flex;align-items:center;gap:6px;padding:4px 8px}#cmp .bar .sp{flex:1}#cmp label{cursor:pointer;color:var(--mut);padding:2px 6px}#send{background:var(--btn);color:#fff;border:0;border-radius:4px;padding:4px 12px;cursor:pointer}
#scrim{position:fixed;inset:0;background:#0009;display:none;place-items:center}#scrim.on{display:grid}
.modal{background:var(--bg);width:520px;max-width:94vw;border-radius:8px;box-shadow:0 18px 48px #0008;max-height:90vh;display:flex;flex-direction:column}
.modal h3{margin:0;padding:18px 24px;border-bottom:1px solid var(--line);font-size:20px}.modal .mb{padding:16px 24px;overflow:auto}.modal .mf{padding:14px 24px;border-top:1px solid var(--line);display:flex;justify-content:flex-end;gap:8px}
.inp{margin-bottom:16px}.inp label{display:block;font-weight:700;margin-bottom:6px}.inp label i{font-weight:400;color:var(--mut)}.inp select,.inp input{width:100%;padding:8px;border:1px solid #8888;border-radius:4px;background:var(--bg);color:var(--tx);font:inherit}.err{color:#e01e5a;font-size:13px;margin-top:4px}
#empty{color:var(--mut);padding:40px;text-align:center}
</style>
<div id=side><div id=ws>…</div><h4>Direct messages</h4><div id=dms></div>
<form id=add><b>Add member</b><input name=id placeholder="U0… id" required><input name=real_name placeholder="Full name" required><input name=email placeholder="email"><button>Add</button></form></div>
<div id=main><div id=hd></div><div id=list></div>
<div id=cmp><textarea id=t placeholder="Message" rows=1></textarea><div class=bar><label title="Attach a file">📎<input type=file id=f hidden></label><span id=fn class=edited></span><span class=sp></span><button id=send>Send</button></div></div></div>
<div id=scrim><div class=modal id=modal></div></div>
<script>
""" + _COMMON_JS + r"""
let S=null,cur=store('fs.member')||'',rev=-1,seen={};
const COLORS=['#e8912d','#2bac76','#1264a3','#cd2553','#9f5fd3','#4a154b'];
const color=s=>COLORS[[...s].reduce((a,c)=>a+c.charCodeAt(0),0)%COLORS.length];
function mrkdwn(t){let h=esc(t);
  h=h.replace(/&lt;(https?:[^|&]+)\|([^&]+)&gt;/g,'<a href="$1" target=_blank>$2</a>').replace(/&lt;(https?:[^&]+)&gt;/g,'<a href="$1" target=_blank>$1</a>');
  h=h.replace(/```([\s\S]*?)```/g,'<code>$1</code>').replace(/`([^`\n]+)`/g,'<code>$1</code>');
  h=h.replace(/(^|[\s(])\*([^*\n]+)\*/g,'$1<b>$2</b>').replace(/(^|[\s(])_([^_\n]+)_/g,'$1<i>$2</i>').replace(/(^|[\s(])~([^~\n]+)~/g,'$1<s>$2</s>');
  return h.replace(/^&gt; ?(.*)$/gm,'<blockquote>$1</blockquote>')}
function blocks(m){if(!(m.blocks||[]).length)return `<div class="txt ${m.text==='_Thinking…_'?'thinking':''}">${mrkdwn(m.text)}</div>`;
  return m.blocks.map(b=>{
    if(b.type==='section')return `<div class="blk txt">${mrkdwn((b.text||{}).text||'')}${(b.fields||[]).map(f=>'<div>'+mrkdwn(f.text)+'</div>').join('')}</div>`;
    if(b.type==='header')return `<div class=blk><b>${esc((b.text||{}).text)}</b></div>`;
    if(b.type==='context')return `<div class="blk edited">${(b.elements||[]).map(e=>mrkdwn(e.text||'')).join(' ')}</div>`;
    if(b.type==='divider')return '<hr>';
    if(b.type==='actions')return `<div class=acts>${(b.elements||[]).filter(e=>e.type==='button').map(e=>e.url?`<a class="sbtn" href="${esc(e.url)}" target=_blank>${esc(e.text.text)}</a>`:`<button class="sbtn ${esc(e.style||'')}" data-ts="${esc(m.id)}" data-a="${esc(e.action_id)}">${esc(e.text.text)}</button>`).join('')}</div>`;
    return `<div class="blk edited">[${esc(b.type)} block]</div>`}).join('')}
function files(m){return (m.files||[]).map(f=>{const u=`/slack/view/${f.id}/${encodeURIComponent(f.name)}`;
  return f.mimetype.startsWith('image/')?`<a class=file href="${u}" target=_blank><img src="${u}"></a>`:`<a class=file href="${u}" target=_blank>📄 ${esc(f.name)}</a>`}).join('<br>')}
function render(d){const L=document.getElementById('list');const at=L.scrollHeight-L.scrollTop-L.clientHeight<80;
  const mem=d.member||{real_name:cur};
  document.getElementById('hd').innerHTML=`${esc(mem.real_name)} <small>DM with Dr. Pretorian (agent) · ${esc(cur)}</small>`;
  if(!d.messages.length){L.innerHTML='<div id=empty>No messages yet. Write to the bot, or wait for it to write first.</div>'}
  else{let prev=null;L.innerHTML=d.messages.map(m=>{const me=m.dir==='in',name=me?mem.real_name:'Dr. Pretorian',cont=prev&&prev.dir===m.dir&&m.ts-prev.ts<300;prev=m;
    return `<div class="msg ${cont?'cont':''}"><div class=av style="background:${me?color(cur):'#4a154b'}">${esc(name[0])}</div><div class=body>${cont?'':`<div class=who><b>${esc(name)}</b>${me?'':'<span class=app>APP</span>'}<span>${hm(m.ts)}</span></div>`}${blocks(m)}${files(m)}${m.edited?'<span class=edited>(edited)</span>':''}${m.thread_ts?'<div class=thr>in thread</div>':''}</div></div>`}).join('')}
  if(at||rev<0)L.scrollTop=L.scrollHeight;modal(d.modal)}
let shownModal='';
function modal(v){const s=document.getElementById('scrim');if(!v){s.classList.remove('on');shownModal='';return}
  if(shownModal===v.id)return;shownModal=v.id;
  document.getElementById('modal').innerHTML=`<h3>${esc((v.title||{}).text)}</h3><div class=mb>${(v.blocks||[]).map(b=>{
    if(b.type==='section')return `<div class="inp txt">${mrkdwn((b.text||{}).text||'')}</div>`;if(b.type!=='input')return '';
    const e=b.element||{},lab=`<label>${esc((b.label||{}).text)}${b.optional?' <i>(optional)</i>':''}</label>`;
    const field=e.type==='static_select'?`<select name="${esc(b.block_id)}"><option value="">Select an option</option>${(e.options||[]).map(o=>`<option value="${esc(o.value)}">${esc(o.text.text)}</option>`).join('')}</select>`
      :`<input name="${esc(b.block_id)}" ${e.multiline?'':''}>`;
    return `<div class=inp>${lab}${field}<div class=err data-e="${esc(b.block_id)}"></div></div>`}).join('')}</div>
    <div class=mf><button class=sbtn id=mc>${esc((v.close||{}).text||'Cancel')}</button><button class="sbtn primary" id=ms>${esc((v.submit||{}).text||'Submit')}</button></div>`;
  s.classList.add('on');
  document.getElementById('mc').onclick=async()=>{await api('/api/slack/modal',{member:cur,cancel:true});poll(true)};
  document.getElementById('ms').onclick=async()=>{const values={};document.querySelectorAll('#modal [name]').forEach(x=>values[x.name]=x.value);
    const r=await api('/api/slack/modal',{member:cur,values});const errs=(r.response||{}).errors||{};
    document.querySelectorAll('#modal .err').forEach(e=>e.textContent=errs[e.dataset.e]||'');if(!Object.keys(errs).length){shownModal='';poll(true)}}}
document.getElementById('list').onclick=async e=>{const b=e.target.closest('button.sbtn');if(!b)return;b.disabled=true;
  await api('/api/slack/tap',{member:cur,ts:b.dataset.ts,action_id:b.dataset.a});b.disabled=false;poll(true)};
async function meta(){S=await (await fetch('/api/state')).json();document.getElementById('ws').textContent=S.slack.team;
  const ids=Object.keys(S.slack.members);if(!cur||!ids.includes(cur))cur=ids[0]||'';
  document.getElementById('dms').innerHTML=ids.map(id=>`<div class="dm ${id===cur?'on':''}" data-id="${id}"><span class=dot></span><span class=n>${esc(S.slack.members[id].real_name)}</span></div>`).join('')}
document.getElementById('dms').onclick=e=>{const d=e.target.closest('.dm');if(!d)return;cur=d.dataset.id;store('fs.member',cur);rev=-1;shownModal='';meta();poll(true)};
document.getElementById('add').onsubmit=async e=>{e.preventDefault();const f=Object.fromEntries(new FormData(e.target));await api('/api/slack/members',f);e.target.reset();cur=f.id;meta();poll(true)};
const T=document.getElementById('t'),F=document.getElementById('f');F.onchange=()=>document.getElementById('fn').textContent=F.files[0]?F.files[0].name:'';
async function send(){const text=T.value.trim(),file=F.files[0];if(!text&&!file)return;const b={member:cur,text};
  if(file){b.media_b64=await fileB64(file);b.filename=file.name;b.mime=file.type}T.value='';F.value='';document.getElementById('fn').textContent='';await api('/api/slack/send',b);poll(true)}
document.getElementById('send').onclick=send;T.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}};
async function poll(force){if(!cur)return;const d=await (await fetch('/api/slack/thread?member='+encodeURIComponent(cur))).json();if(force||d.rev!==rev){render(d);rev=d.rev}}
meta().then(()=>poll(true));setInterval(poll,1200);setInterval(meta,8000);
</script>"""

TELEGRAM_HTML = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Fake Telegram</title>
<style>
:root{--bg:#fff;--chat:#8fb87a;--pat:#c4d9a8;--in:#fff;--out:#effdde;--tx:#000;--mut:#707579;--acc:#3390ec;--line:#dadce0;--sel:#3390ec;--kb:#0000003a;--kbt:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#212121;--chat:#0f0f0f;--pat:#1a2a1a;--in:#212121;--out:#766ac8;--tx:#fff;--mut:#aaa;--line:#303030;--sel:#766ac8;--kb:#ffffff22;--kbt:#fff}}
*{box-sizing:border-box}body{margin:0;height:100vh;display:flex;font:15px/1.38 Roboto,-apple-system,"Segoe UI",sans-serif;color:var(--tx);background:var(--bg)}
#side{width:300px;flex:none;border-right:1px solid var(--line);display:flex;flex-direction:column;background:var(--bg)}
#side .top{padding:12px 16px;font-weight:500;font-size:17px;border-bottom:1px solid var(--line)}
.chat{display:flex;gap:12px;align-items:center;padding:9px 12px;cursor:pointer;border-radius:10px;margin:2px 6px}.chat:hover{background:#8881}.chat.on{background:var(--sel);color:#fff}.chat.on small{color:#fffc}
.av{width:48px;height:48px;border-radius:50%;flex:none;display:grid;place-items:center;color:#fff;font-weight:500;font-size:18px}
.chat div{min-width:0}.chat small{color:var(--mut);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#side form{margin:auto 12px 12px;display:flex;flex-direction:column;gap:4px;font-size:12px}#side input{border:1px solid var(--line);border-radius:6px;padding:5px;background:var(--bg);color:var(--tx)}#side button{border:0;border-radius:6px;padding:6px;background:var(--acc);color:#fff;cursor:pointer}
#main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--chat);background-image:radial-gradient(var(--pat) 1px,transparent 1px);background-size:22px 22px}
#hd{background:var(--bg);padding:8px 16px;display:flex;gap:12px;align-items:center;border-bottom:1px solid var(--line)}#hd .av{width:40px;height:40px;font-size:16px}#hd small{color:var(--mut);display:block}#hd small.ty{color:var(--acc)}
#list{flex:1;overflow-y:auto;padding:12px 8%;display:flex;flex-direction:column;gap:4px}
.row{display:flex;flex-direction:column;align-items:flex-start}.row.in{align-items:flex-end}
.b{max-width:min(480px,80%);background:var(--in);border-radius:12px;padding:6px 10px 6px;box-shadow:0 1px 2px #0002;position:relative;white-space:pre-wrap;overflow-wrap:anywhere}.in .b{background:var(--out)}
.b .t{float:right;font-size:12px;color:var(--mut);margin:6px 0 -4px 10px}.b a{color:var(--acc)}.b code,.b pre{font-family:ui-monospace,monospace;font-size:13px}.b blockquote{margin:2px 0;border-left:3px solid var(--acc);padding-left:8px}
.kb{display:grid;gap:4px;margin-top:4px;max-width:min(480px,80%);width:100%}.kb .r{display:flex;gap:4px}.kb button{flex:1;border:0;border-radius:8px;background:var(--kb);color:var(--kbt);backdrop-filter:blur(6px);padding:9px;font:inherit;font-weight:500;cursor:pointer;text-shadow:0 1px 2px #0005}
.rx{font-size:13px;margin-top:2px}.reply{border-left:3px solid var(--acc);padding-left:6px;margin-bottom:3px;font-size:13px;color:var(--acc)}
.att img{max-width:300px;border-radius:8px;display:block}.att a{display:inline-block;margin-bottom:4px}
#cmp{display:flex;gap:8px;padding:8px 8% 16px;align-items:flex-end}#cmp .fld{flex:1;display:flex;align-items:center;background:var(--bg);border-radius:12px;padding:0 8px}#cmp textarea{flex:1;border:0;outline:0;resize:none;background:none;color:var(--tx);font:inherit;padding:12px 4px}
#cmp label{cursor:pointer;color:var(--mut);font-size:20px}#send{width:52px;height:52px;border-radius:50%;border:0;background:var(--acc);color:#fff;font-size:20px;cursor:pointer}
#start{margin:auto;background:var(--bg);border:0;border-radius:12px;padding:14px 60px;font:inherit;font-weight:500;color:var(--acc);cursor:pointer;text-transform:uppercase}
#toast{position:fixed;top:70px;left:50%;transform:translateX(-50%);background:#000c;color:#fff;border-radius:10px;padding:10px 18px;display:none}#toast.on{display:block}
.hint{align-self:center;background:#0004;color:#fff;border-radius:12px;padding:3px 10px;font-size:13px;margin:6px 0}
</style>
<div id=side><div class=top>Telegram</div><div id=chats></div>
<form id=add><b>Add user</b><input name=id placeholder="numeric id, e.g. 7100000002" required><input name=first_name placeholder="First name" required><input name=last_name placeholder="Last name"><input name=username placeholder="username"><button>Add</button></form></div>
<div id=main><div id=hd></div><div id=list></div>
<div id=cmp><div class=fld><label title="Attach">📎<input type=file id=f hidden></label><textarea id=t rows=1 placeholder="Message"></textarea><small id=fn></small></div><button id=send>➤</button></div></div>
<div id=toast></div>
<script>
""" + _COMMON_JS + r"""
let S=null,cur=store('ft.user')||'',rev=-1,lastToast='';
const COLORS=['#e17076','#7bc862','#e5ca77','#65aadd','#a695e7','#ee7aae','#6ec9cb','#faa774'];
const color=s=>COLORS[[...String(s)].reduce((a,c)=>a+c.charCodeAt(0),0)%COLORS.length];
const ALLOWED={B:1,STRONG:1,I:1,EM:1,U:1,INS:1,S:1,STRIKE:1,DEL:1,CODE:1,PRE:1,A:1,BLOCKQUOTE:1,'TG-SPOILER':1,SPAN:1};
function safeHtml(src){const doc=new DOMParser().parseFromString('<div>'+src+'</div>','text/html');
  const walk=n=>[...n.childNodes].map(c=>{if(c.nodeType===3)return esc(c.textContent);if(c.nodeType!==1)return '';const t=c.tagName,inner=walk(c);
    if(!ALLOWED[t])return inner;if(t==='A'){const h=c.getAttribute('href')||'';return /^https?:|^tg:/.test(h)?`<a href="${esc(h)}" target=_blank>${inner}</a>`:inner}
    if(t==='TG-SPOILER'||t==='SPAN')return `<span style="background:#8886">${inner}</span>`;return `<${t.toLowerCase()}>${inner}</${t.toLowerCase()}>`}).join('');
  return walk(doc.body.firstChild)}
function body(m){return m.parse_mode==='HTML'?safeHtml(m.text||''):esc(m.text||'')}
function atts(m){return (m.files||[]).map(f=>{const u='/telegram/view/'+f.file_id;return f.kind==='photo'?`<div class=att><img src="${u}"></div>`:f.kind==='voice'?`<div class=att><audio controls src="${u}"></audio></div>`:`<div class=att><a href="${u}" target=_blank>📄 ${esc(f.name)}</a></div>`}).join('')}
function render(d){const L=document.getElementById('list'),at=L.scrollHeight-L.scrollTop-L.clientHeight<80,u=d.user||{first_name:cur};
  const name=(u.first_name+' '+(u.last_name||'')).trim(),bot=d.bot||{};
  document.getElementById('hd').innerHTML=`<div class=av style="background:#3390ec">${esc((bot.first_name||'B')[0])}</div><div><b>${esc(bot.first_name)}</b><small class="${d.typing?'ty':''}">${d.typing?'typing…':'bot · @'+esc(bot.username)}</small></div><div style="margin-left:auto;color:var(--mut);font-size:13px">you are ${esc(name)} · ${esc(cur)}</div>`;
  const byId=Object.fromEntries(d.messages.map(m=>[m.id,m]));
  L.innerHTML=d.messages.length?d.messages.map(m=>{const kb=((m.reply_markup||{}).inline_keyboard||[]);
    return `<div class="row ${m.dir}"><div class=b>${m.reply_to&&byId[m.reply_to]?`<div class=reply>${esc((byId[m.reply_to].text||'').slice(0,60))}</div>`:''}${atts(m)}${body(m)}<span class=t>${m.edited?'edited ':''}${hm(m.ts)}${m.dir==='in'?' ✓✓':''}</span></div>${(m.reactions||[]).length?`<div class=rx>${m.reactions.join(' ')}</div>`:''}${kb.length?`<div class=kb>${kb.map(r=>`<div class=r>${r.map(btn=>btn.url?`<button onclick="window.open('${esc(btn.url)}')">${esc(btn.text)} ↗</button>`:`<button data-m="${m.id}" data-d="${esc(btn.callback_data)}">${esc(btn.text)}</button>`).join('')}</div>`).join('')}</div>`:''}</div>`}).join('')
    :`<div class=hint>${esc(bot.first_name)} has not been started by this user.<br>A bot cannot write first until the user presses START.</div><button id=start>Start</button>`;
  const st=document.getElementById('start');if(st)st.onclick=()=>sendText('/start');
  if(at||rev<0)L.scrollTop=L.scrollHeight;
  if(d.toast&&d.toast.id!==lastToast){lastToast=d.toast.id;const t=document.getElementById('toast');t.textContent=d.toast.text;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),2500)}}
document.getElementById('list').onclick=async e=>{const b=e.target.closest('button[data-d]');if(!b)return;b.disabled=true;
  await api('/api/telegram/tap',{user:cur,message_id:+b.dataset.m,data:b.dataset.d});b.disabled=false;poll(true)};
async function meta(){S=await (await fetch('/api/state')).json();const ids=Object.keys(S.telegram.users);if(!cur||!ids.includes(cur))cur=ids[0]||'';
  document.getElementById('chats').innerHTML=ids.map(id=>{const u=S.telegram.users[id],n=(u.first_name+' '+(u.last_name||'')).trim();
    return `<div class="chat ${id===cur?'on':''}" data-id="${id}"><div class=av style="background:${color(id)}">${esc(n[0])}</div><div><b>${esc(n)}</b><small>@${esc(u.username||id)} → ${esc(S.telegram.bot.username)}</small></div></div>`}).join('')
    +(S.telegram.webhook_url?'':'<p style="padding:0 16px;color:#e53935;font-size:13px">No webhook set yet: connect Telegram on the agent so the runtime calls setWebhook.</p>')}
document.getElementById('chats').onclick=e=>{const c=e.target.closest('.chat');if(!c)return;cur=c.dataset.id;store('ft.user',cur);rev=-1;meta();poll(true)};
document.getElementById('add').onsubmit=async e=>{e.preventDefault();const f=Object.fromEntries(new FormData(e.target));await api('/api/telegram/users',f);e.target.reset();cur=f.id;meta();poll(true)};
const T=document.getElementById('t'),F=document.getElementById('f');F.onchange=()=>document.getElementById('fn').textContent=F.files[0]?F.files[0].name:'';
async function sendText(text){await api('/api/telegram/send',{user:cur,text});poll(true)}
async function send(){const text=T.value.trim(),file=F.files[0];if(!text&&!file)return;const b={user:cur,text};
  if(file){b.media_b64=await fileB64(file);b.filename=file.name;b.mime=file.type}T.value='';F.value='';document.getElementById('fn').textContent='';await api('/api/telegram/send',b);poll(true)}
document.getElementById('send').onclick=send;T.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}};
async function poll(force){if(!cur)return;const d=await (await fetch('/api/telegram/thread?user='+encodeURIComponent(cur))).json();if(force||d.rev!==rev||d.typing||d.toast){render(d);rev=d.rev}}
meta().then(()=>poll(true));setInterval(poll,1200);setInterval(meta,8000);
</script>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slack-signing-secret", required=True, help="the Signing Secret entered on the agent's Slack channel")
    parser.add_argument("--slack-events-base", default="", help="where the runtime's channel webhooks listen (default http://127.0.0.1:8003)")
    parser.add_argument("--port", type=int, default=58124)
    parser.add_argument("--state", default="", help="JSON file that keeps members, users, the webhook and chats across restarts")
    parser.add_argument("--fixtures", default="", help="directory the script API's ``fixture`` names resolve in")
    args = parser.parse_args()
    global STATE_FILE, FIXTURES, SLACK_SIGNING_SECRET, _ids
    STATE_FILE, FIXTURES, SLACK_SIGNING_SECRET = args.state, args.fixtures, args.slack_signing_secret
    if STATE_FILE and os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state.update(json.load(f))
        # Telegram message ids and update ids must keep growing across restarts.
        _ids = itertools.count(10_000 + len(state["messages"]) * 10 + state["rev"])
    if args.slack_events_base:
        state["slack"]["events_base"] = args.slack_events_base
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
