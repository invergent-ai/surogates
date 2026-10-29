"""A rehearsal world's Meta Graph API, called by the runtime's real WhatsApp adapter.

Mounted at ``/w/{phone_number_id}/whatsapp``: ``graph_base`` sends a rehearsal
number here, so the adapter's ``/messages``, ``/media`` and media lookups land
on the world that owns the number.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response

from surogates.rehearsal import store
from surogates.rehearsal.webhook import post_webhook

router = APIRouter(prefix="/w/{phone_number_id}/whatsapp")


def _graph_error(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "OAuthException", "code": code}},
                        status_code=status)


def _unauthorized() -> JSONResponse:
    return _graph_error(401, 190, "Invalid OAuth access token.")


async def _authorized_run(request: Request, phone_number_id: str):
    run = await store.run_by_number(request.app.state.session_factory, phone_number_id)
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if run is None or run.status != "running" or not secrets.compare_digest(token, run.access_token):
        return None
    return run


async def _report_statuses(app, run, wamid: str, recipient: str) -> None:
    sf, settings = app.state.session_factory, app.state.settings
    for status in ("sent", "delivered"):
        await store.set_status(sf, run.id, wamid, status)
        await post_webhook(
            app.state.http, channels_url=settings.rehearsal.channels_url,
            phone_number_id=run.phone_number_id, app_secret=run.app_secret,
            value={"statuses": [{"id": wamid, "status": status, "recipient_id": recipient,
                                 "timestamp": str(int(datetime.now(timezone.utc).timestamp()))}]},
        )


# Declared before ``/{version}/{media_id}`` so the two-segment path is not
# read as a media lookup.
@router.get("/media-bytes/{media_id}")
async def media_bytes(phone_number_id: str, media_id: str, request: Request):
    run = await _authorized_run(request, phone_number_id)
    if run is None:
        return _unauthorized()
    media = await store.get_media(request.app.state.session_factory, run.id, media_id)
    if media is None:
        return _graph_error(404, 100, "Unsupported get request.")
    return Response(content=media.data, media_type=media.mime_type)


@router.post("/{version}/{pnid}/media")
async def upload_media(phone_number_id: str, version: str, pnid: str, request: Request):
    run = await _authorized_run(request, phone_number_id)
    if run is None or pnid != phone_number_id:
        return _unauthorized()
    upload = (await request.form())["file"]
    media_id = await store.put_media(request.app.state.session_factory, run.id, await upload.read(),
                                     upload.content_type or "application/octet-stream")
    return {"id": media_id}


@router.get("/{version}/{media_id}")
async def media_meta(phone_number_id: str, version: str, media_id: str, request: Request):
    run = await _authorized_run(request, phone_number_id)
    if run is None:
        return _unauthorized()
    media = await store.get_media(request.app.state.session_factory, run.id, media_id)
    if media is None:
        return _graph_error(404, 100, "Unsupported get request.")
    base = request.app.state.settings.rehearsal.url.rstrip("/")
    return {"url": f"{base}/w/{phone_number_id}/whatsapp/media-bytes/{media_id}",
            "mime_type": media.mime_type, "file_size": len(media.data), "id": media_id}


@router.post("/{version}/{pnid}/messages")
async def send(phone_number_id: str, version: str, pnid: str, request: Request,
               background: BackgroundTasks):
    run = await _authorized_run(request, phone_number_id)
    if run is None or pnid != phone_number_id:
        return _unauthorized()
    sf = request.app.state.session_factory
    body = await request.json()
    if body.get("status") == "read":
        message_id = str(body.get("message_id") or "")
        await store.mark_read_up_to(sf, run.id, message_id)
        if body.get("typing_indicator"):
            await store.set_typing(sf, run.id, message_id, True)
        return {"success": True}
    to = str(body.get("to") or "").lstrip("+")
    person = await store.person_by_address(sf, run.id, to)
    if person is None:
        return _graph_error(400, 131026, "Message undeliverable: the recipient is not on WhatsApp.")
    kind = str(body.get("type") or "text")
    if kind != "template" and not store.window_open(
        await store.thread(sf, person.id), datetime.now(timezone.utc),
    ):
        return _graph_error(400, 131047, "Re-engagement message: more than 24 hours have passed "
                                         "since the recipient last replied")
    wamid = f"wamid.R{secrets.token_hex(10)}"
    await store.log_message(sf, run_id=run.id, person_id=person.id, direction="out", kind=kind,
                            body=body, wamid=wamid, status="accepted")
    await store.set_typing(sf, run.id, wamid, False)
    background.add_task(_report_statuses, request.app, run, wamid, to)
    return {"messaging_product": "whatsapp", "contacts": [{"input": to, "wa_id": to}],
            "messages": [{"id": wamid}]}
