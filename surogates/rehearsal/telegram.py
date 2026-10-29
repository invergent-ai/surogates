"""A rehearsal world's Telegram Bot API, called by the runtime's real Telegram adapter.

Mounted at ``/t``: the adapter sends a rehearsal bot's ``/bot{token}/{method}``
calls here, so what it sends, edits and reacts with lands in the run that owns
the bot.  Answers keep the Bot API's ``{"ok", "result"}`` / ``{"ok": false,
"error_code", "description"}`` shape.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from surogates.rehearsal import store

router = APIRouter(prefix="/t")


def _error(code: int, description: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error_code": code, "description": description},
                        status_code=code)


def _ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def _bot(run) -> dict[str, Any]:
    return {"id": int(run.access_token.split(":", 1)[0]), "is_bot": True,
            "first_name": run.phone_number_id, "username": run.phone_number_id}


async def _authorized_run(request: Request, token: str):
    run = await store.run_by_token(request.app.state.session_factory, token)
    if (run is None or run.channel != "telegram" or run.status != "running"
            or not secrets.compare_digest(token, run.access_token)):
        return None
    return run


async def _person(sf, run, chat_id: Any):
    return await store.person_by_address(sf, run.id, str(chat_id or ""))


@router.post("/bot{token}/{method}")
async def call(token: str, method: str, request: Request):
    run = await _authorized_run(request, token)
    if run is None:
        return _error(401, "Unauthorized")
    sf = request.app.state.session_factory
    try:
        body: dict[str, Any] = await request.json()
    except ValueError:
        body = {}

    if method in ("setWebhook", "deleteWebhook", "answerCallbackQuery"):
        return _ok(True)
    if method == "getMe":
        return _ok(_bot(run))

    person = await _person(sf, run, body.get("chat_id"))
    if person is None:
        return _error(400, "Bad Request: chat not found")
    chat = {"id": int(person.address), "type": "private", "first_name": person.name}

    if method == "sendChatAction":
        await store.set_person_typing(sf, person.id, body.get("action") == "typing")
        return _ok(True)

    if method == "sendMessage":
        message_id = await store.next_message_id(sf, run.id)
        message = {
            "message_id": message_id, "from": _bot(run), "chat": chat,
            "date": int(datetime.now(timezone.utc).timestamp()),
            **{k: body[k] for k in ("text", "parse_mode", "reply_markup") if k in body},
        }
        if body.get("reply_to_message_id"):
            message["reply_to_message"] = {"message_id": int(body["reply_to_message_id"])}
        await store.log_message(sf, run_id=run.id, person_id=person.id, direction="out",
                                kind="text", body=message, wamid=str(message_id), status="sent")
        await store.set_person_typing(sf, person.id, False)
        return _ok(message)

    if method in ("editMessageText", "editMessageReplyMarkup"):
        wamid = str(body.get("message_id") or "")
        sent = await store.message(sf, run.id, wamid)
        if sent is None or sent.direction != "out":
            return _error(400, "Bad Request: message to edit not found")
        edited = {**sent.body, "edit_date": int(datetime.now(timezone.utc).timestamp())}
        if method == "editMessageText":
            edited |= {k: body[k] for k in ("text", "parse_mode") if k in body}
        edited.pop("reply_markup", None)
        if body.get("reply_markup"):
            edited["reply_markup"] = body["reply_markup"]
        await store.replace_body(sf, run.id, wamid, edited)
        return _ok(edited)

    if method == "setMessageReaction":
        emoji = next((r.get("emoji") for r in body.get("reaction") or [] if r.get("emoji")), "")
        await store.log_message(sf, run_id=run.id, person_id=person.id, direction="out",
                                kind="reaction",
                                body={"reaction": {"message_id": str(body.get("message_id")),
                                                   "emoji": emoji}},
                                wamid=f"reaction{secrets.token_hex(6)}", status="sent")
        return _ok(True)

    return _error(400, f"Bad Request: {method} is not available in a rehearsal yet")
