"""A fake Meta Graph API plus a browser "phone", for testing WhatsApp locally.

Point both services at it:

    SUROGATES_WHATSAPP_GRAPH_BASE=http://127.0.0.1:58123   (runtime)
    SUROGATE_WHATSAPP_GRAPH_BASE=http://127.0.0.1:58123    (ops)

then connect WhatsApp in Studio with any token starting ``EAA`` (100+ chars),
WABA id ``1234567890`` and the app secret given below, and open
http://127.0.0.1:58123/phone to write as a patient or a doctor.

Inbound messages are signed with ``--app-secret`` and posted to the
callback that ops registered (``override_callback_uri``), exactly like Meta.
Templates are approved on creation.  Flows (``/{waba}/flows``, ``/{flow}/assets``,
``/{flow}/publish``) are kept with their Flow JSON, and a flow message or a
template's FLOW button opens on the phone as a form whose submission comes
back as an ``nfm_reply``, like Meta's.  State lives in memory.  ``--fixtures DIR`` lists
files the phone can attach (its first audio file is what the mic button sends), and
``/phone?wa=40712000001,40712000002`` picks which conversations are open side by side.

    .venv/bin/python scripts/fake_whatsapp.py --app-secret 0123456789abcdef0123456789abcdef
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import itertools
import json
import os
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

PNID = "100000000000001"
DISPLAY_PHONE = "+40 700 000 001"
APP_ID = "424242424242424"

app = FastAPI()
_ids = itertools.count(1)
state: dict = {
    "app_secret": "",
    "callback": "",
    "verify_token": "",
    "verified_name": "Dr. Pretorian · Urmărire TAVI",
    "templates": {},
    "flows": {},
    "messages": [],
}


STATE_FILE: str = ""
FIXTURES: str = ""


def _persist() -> None:
    if STATE_FILE:
        with open(STATE_FILE, "w") as f:
            json.dump({k: v for k, v in state.items() if k != "app_secret"}, f)


def _wamid() -> str:
    return f"wamid.FAKE{next(_ids):08d}"


def _log(direction: str, wa_id: str, kind: str, body: dict) -> dict:
    entry = {
        "id": body.get("id") or _wamid(),
        "dir": direction,
        "wa_id": wa_id,
        "kind": kind,
        "body": body,
        "ts": time.time(),
        "status": "sent" if direction == "out" else "",
    }
    state["messages"].append(entry)
    _persist()
    return entry


@app.get("/{version}/debug_token")
async def debug_token(version: str):
    return {"data": {"app_id": APP_ID, "is_valid": True}}


@app.get("/{version}/{waba_id}/phone_numbers")
async def phone_numbers(version: str, waba_id: str):
    return {"data": [{
        "id": PNID,
        "display_phone_number": DISPLAY_PHONE,
        "verified_name": state["verified_name"],
        "code_verification_status": "VERIFIED",
        "quality_rating": "GREEN",
    }]}


@app.get("/{version}/{waba_id}/subscribed_apps")
async def subscribed(version: str, waba_id: str):
    return {"data": [{"whatsapp_business_api_data": {"id": APP_ID}}]}


@app.post("/{version}/{waba_id}/subscribed_apps")
async def subscribe(version: str, waba_id: str, request: Request):
    form = dict(request.query_params)
    try:
        form.update(await request.json())
    except Exception:
        form.update(dict(await request.form()))
    uri = form.get("override_callback_uri")
    if uri:
        token = form.get("verify_token", "")
        challenge = str(next(_ids))
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(uri, params={
                "hub.mode": "subscribe",
                "hub.verify_token": token,
                "hub.challenge": challenge,
            })
        if r.text.strip() != challenge:
            return _error(2200, f"Callback verification failed: {r.status_code} {r.text[:200]}")
        state["callback"], state["verify_token"] = uri, token
        _persist()
    return {"success": True}


@app.get("/{version}/{waba_id}/message_templates")
async def list_templates(version: str, waba_id: str):
    return {"data": list(state["templates"].values()), "paging": {}}


@app.post("/{version}/{waba_id}/message_templates")
async def create_template(version: str, waba_id: str, request: Request):
    body = await request.json()
    key = f"{body['name']}:{body['language']}"
    if key in state["templates"]:
        return _error(100, "Content in this language already exists", sub=2388024)
    template = {
        "id": str(next(_ids)),
        "name": body["name"],
        "language": body["language"],
        "category": body.get("category", "UTILITY"),
        "status": "APPROVED",
        "components": body.get("components", []),
    }
    state["templates"][key] = template
    _persist()
    return {"id": template["id"], "status": "APPROVED", "category": template["category"]}


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------

_FLOW_ROW = ("id", "name", "status", "categories", "validation_errors")


async def _body(request: Request) -> dict:
    try:
        return dict(await request.json())
    except Exception:
        return dict(await request.form())


def _validation_errors(flow_json) -> list[dict]:
    if not isinstance(flow_json, dict) or not flow_json.get("screens"):
        return [{"error": "INVALID_PROPERTY", "error_type": "JSON_SCHEMA_ERROR",
                 "message": "The flow JSON needs a version and at least one screen.",
                 "line_start": 1, "line_end": 1, "column_start": 1, "column_end": 1}]
    return []


def _store_flow_json(flow: dict, raw) -> None:
    try:
        flow_json = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        flow_json = None
    flow["flow_json"] = flow_json if isinstance(flow_json, dict) else None
    flow["validation_errors"] = _validation_errors(flow_json)


@app.post("/{version}/{waba_id}/flows")
async def create_flow(version: str, waba_id: str, request: Request):
    b = await _body(request)
    categories = b.get("categories") or ["OTHER"]
    if isinstance(categories, str):
        categories = json.loads(categories)
    flow = {"id": str(next(_ids)), "name": b.get("name") or "flow", "status": "DRAFT",
            "categories": categories, "validation_errors": [], "flow_json": None, "waba_id": waba_id}
    if b.get("flow_json"):
        _store_flow_json(flow, b["flow_json"])
    if str(b.get("publish")).lower() == "true" and flow["flow_json"] and not flow["validation_errors"]:
        flow["status"] = "PUBLISHED"
    state["flows"][flow["id"]] = flow
    _persist()
    return {"id": flow["id"], "success": True, "validation_errors": flow["validation_errors"]}


@app.get("/{version}/{waba_id}/flows")
async def list_flows(version: str, waba_id: str):
    rows = [{k: f[k] for k in _FLOW_ROW} for f in state["flows"].values() if f.get("waba_id") == waba_id]
    return {"data": rows, "paging": {"cursors": {"before": "", "after": ""}}}


@app.post("/{version}/{flow_id}/assets")
async def upload_flow_json(version: str, flow_id: str, request: Request):
    flow = state["flows"].get(flow_id)
    if flow is None:
        return _error(100, f"Unsupported post request. Object with ID '{flow_id}' does not exist")
    if flow["status"] != "DRAFT":
        return _error(139001, "Flow can't be updated: only a draft flow can be changed")
    form = await request.form()
    upload = form.get("file")
    _store_flow_json(flow, await upload.read() if hasattr(upload, "read") else upload)
    _persist()
    return {"success": True, "validation_errors": flow["validation_errors"]}


@app.get("/{version}/{flow_id}/assets")
async def flow_assets(version: str, flow_id: str, request: Request):
    flow = state["flows"].get(flow_id)
    if flow is None or flow["flow_json"] is None:
        return {"data": [], "paging": {}}
    return {"data": [{"name": "flow.json", "asset_type": "FLOW_JSON",
                      "download_url": f"{request.base_url}flow-json/{flow_id}"}], "paging": {}}


@app.get("/flow-json/{flow_id}")
async def flow_json_download(flow_id: str):
    return state["flows"][flow_id]["flow_json"]


@app.post("/{version}/{flow_id}/publish")
async def publish_flow(version: str, flow_id: str):
    flow = state["flows"].get(flow_id)
    if flow is None:
        return _error(100, f"Object with ID '{flow_id}' does not exist")
    if flow["flow_json"] is None or flow["validation_errors"]:
        return _error(139002, "Publishing failed: the flow JSON is missing or has validation errors")
    flow["status"] = "PUBLISHED"
    _persist()
    return {"success": True}


@app.post("/{version}/{flow_id}/deprecate")
async def deprecate_flow(version: str, flow_id: str):
    flow = state["flows"].get(flow_id)
    if flow is None or flow["status"] != "PUBLISHED":
        return _error(139003, "Only a published flow can be deprecated")
    flow["status"] = "DEPRECATED"
    _persist()
    return {"success": True}


def _flow_fields(flow: dict, request: Request) -> dict:
    wanted = (request.query_params.get("fields") or "id,name").split(",")
    out = {"id": flow["id"]}
    for field in wanted:
        if field in _FLOW_ROW:
            out[field] = flow[field]
        elif field == "json_version":
            out[field] = (flow["flow_json"] or {}).get("version")
        elif field == "preview":
            out[field] = {"preview_url": f"{request.base_url}phone", "expires_at": "2099-01-01T00:00:00+0000"}
    return out


def _resolve(expr, values: dict, data: dict):
    """A Flow JSON dynamic value: ``${form.x}``, ``${screen.S.form.x}`` or ``${data.x}``."""
    if not (isinstance(expr, str) and expr.startswith("${") and expr.endswith("}")):
        return expr
    path = expr[2:-1].split(".")
    if path[0] == "form" or (path[0] == "screen" and len(path) > 3 and path[2] == "form"):
        return values.get(path[-1])
    if path[0] == "data":
        # Earlier screens' answers reach a later screen as its data.
        return values.get(path[-1], data.get(path[-1]))
    return None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for child in node.get("children") or []:
            yield from _walk(child)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def flow_response(flow_json: dict, values: dict, data: dict, token: str) -> dict:
    """What Meta returns as ``response_json``: the terminal ``complete`` payload plus the token."""
    payload = None
    for screen in flow_json.get("screens") or []:
        for node in _walk((screen.get("layout") or {}).get("children") or []):
            action = node.get("on-click-action") or {}
            if action.get("name") == "complete":
                payload = {k: _resolve(v, values, data) for k, v in (action.get("payload") or {}).items()}
    if payload is None:
        payload = dict(values)
    return {**{k: v for k, v in payload.items() if v is not None}, "flow_token": token}


@app.get("/phone/api/flow/{flow_id}")
async def phone_flow(flow_id: str):
    return state["flows"][flow_id]["flow_json"]


@app.post("/phone/api/flow_submit")
async def flow_submit(request: Request):
    """The person filled the form in: post the ``nfm_reply`` webhook Meta sends."""
    b = await request.json()
    flow_json = state["flows"][b["flow_id"]]["flow_json"]
    response = flow_response(flow_json, b.get("values") or {}, b.get("data") or {}, b.get("flow_token") or "unused")
    wa_id, name = b["wa_id"], b.get("name") or b["wa_id"]
    message = {
        "from": wa_id, "id": _wamid(), "timestamp": str(int(time.time())), "type": "interactive",
        "interactive": {"type": "nfm_reply", "nfm_reply": {
            "name": "flow", "body": "Sent", "response_json": json.dumps(response, ensure_ascii=False),
        }},
    }
    if b.get("context_id"):
        message["context"] = {"from": DISPLAY_PHONE.replace(" ", "").lstrip("+"), "id": b["context_id"]}
    entry = _log("in", wa_id, "interactive", message)
    await _inbound([entry], {"contacts": [{"profile": {"name": name}, "wa_id": wa_id}], "messages": [message]})
    return {"ok": True, "response_json": response}


MEDIA: dict[str, tuple[bytes, str]] = {}


@app.get("/media-bytes/{media_id}")
async def media_bytes(media_id: str):
    from fastapi.responses import Response
    if media_id not in MEDIA:
        return Response(status_code=404)
    data, mime = MEDIA[media_id]
    return Response(data, media_type=mime)


@app.get("/{version}/{media_id}")
async def media_url(version: str, media_id: str, request: Request):
    if media_id in state["flows"]:
        return _flow_fields(state["flows"][media_id], request)
    data, mime = MEDIA[media_id]
    return {"url": f"{request.base_url}media-bytes/{media_id}", "mime_type": mime,
            "file_size": len(data), "id": media_id, "messaging_product": "whatsapp"}


@app.post("/{version}/{pnid}/media")
async def upload_media(version: str, pnid: str):
    return {"id": f"media{next(_ids)}"}


@app.post("/{version}/{pnid}/messages")
async def send(version: str, pnid: str, request: Request):
    body = await request.json()
    if body.get("status") == "read":
        _mark_read(body.get("message_id"))
        if body.get("typing_indicator"):
            state.setdefault("typing", []).append({"ts": time.time(), "message_id": body.get("message_id")})
        _persist()
        return {"success": True}
    wa_id = str(body.get("to", "")).lstrip("+")
    kind = body.get("type", "text")
    if kind == "template":
        t = body.get("template") or {}
        key = f"{t.get('name')}:{(t.get('language') or {}).get('code')}"
        if key not in state["templates"]:
            return _error(132001, f"Template name does not exist in the translation ({key})")
    elif not _window_open(wa_id):
        return _error(131047, "Re-engagement message: more than 24 hours have passed since the recipient last replied")
    elif kind == "interactive" and (body.get("interactive") or {}).get("type") == "flow":
        flow_id = (((body["interactive"].get("action") or {}).get("parameters")) or {}).get("flow_id")
        if (state["flows"].get(str(flow_id)) or {}).get("status") != "PUBLISHED":
            return _error(100, f"Invalid parameter: flow {flow_id} is not a published flow")
    entry = _log("out", wa_id, kind, body)
    asyncio.create_task(_statuses(entry))
    return {"messaging_product": "whatsapp", "contacts": [{"input": wa_id, "wa_id": wa_id}],
            "messages": [{"id": entry["id"]}]}


def _mark_read(message_id) -> None:
    """A read receipt turns that message, and every earlier one from the person, blue."""
    target = next((m for m in state["messages"] if m.get("id") == message_id and m["dir"] == "in"), None)
    if target:
        for m in state["messages"]:
            if m["dir"] == "in" and m["wa_id"] == target["wa_id"] and m["ts"] <= target["ts"]:
                m["status"] = "read"


async def _inbound(entries: list[dict], value: dict) -> None:
    """Post the person's messages to the business; a webhook that got through means delivered."""
    await _post_webhook(value)
    if state["callback"]:
        for entry in entries:
            entry["status"] = entry["status"] or "delivered"
        _persist()


def _window_open(wa_id: str) -> bool:
    last = max((m["ts"] for m in state["messages"] if m["dir"] == "in" and m["wa_id"] == wa_id), default=0)
    return time.time() - last < 24 * 3600


def _error(code: int, message: str, sub: int | None = None):
    from fastapi.responses import JSONResponse
    err = {"message": message, "type": "OAuthException", "code": code}
    if sub:
        err["error_subcode"] = sub
    return JSONResponse({"error": err}, status_code=400)


async def _statuses(entry: dict) -> None:
    for status in ("sent", "delivered"):
        await asyncio.sleep(0.5)
        entry["status"] = status
        await _post_webhook({"statuses": [{
            "id": entry["id"], "status": status, "timestamp": str(int(time.time())),
            "recipient_id": entry["wa_id"],
        }]})


async def _post_webhook(value: dict) -> None:
    if not state["callback"]:
        return
    payload = {"object": "whatsapp_business_account", "entry": [{
        "id": "1234567890",
        "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": DISPLAY_PHONE.replace(" ", "").lstrip("+"),
                         "phone_number_id": PNID},
            **value,
        }}],
    }]}
    raw = json.dumps(payload).encode()
    sig = hmac.new(state["app_secret"].encode(), raw, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(state["callback"], content=raw, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={sig}",
        })


@app.post("/phone/api/send")
async def phone_send(request: Request):
    """Simulate the person: text, a button tap, a list pick, or a template quick reply."""
    b = await request.json()
    wa_id, name = b["wa_id"], b.get("name") or b["wa_id"]
    message: dict = {"from": wa_id, "id": _wamid(), "timestamp": str(int(time.time()))}
    if b.get("button_reply"):
        message |= {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": b["button_reply"]}}
    elif b.get("list_reply"):
        message |= {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": b["list_reply"]}}
    elif b.get("media_path") or b.get("fixture") or b.get("media_b64"):
        import base64, mimetypes, pathlib
        if b.get("media_b64"):
            path, raw = pathlib.Path(b.get("filename") or "upload.bin"), base64.b64decode(b["media_b64"])
        else:
            path = pathlib.Path(FIXTURES, pathlib.Path(b["fixture"]).name) if b.get("fixture") else pathlib.Path(b["media_path"])
            raw = path.read_bytes()
        mime = b.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        kind = b.get("kind") or ("image" if mime.startswith("image/") else "audio" if mime.startswith("audio/") else "document")
        media_id = f"media{next(_ids)}"
        MEDIA[media_id] = (raw, mime)
        block = {"id": media_id, "mime_type": mime}
        if b.get("caption"):
            block["caption"] = b["caption"]
        if kind == "document":
            block["filename"] = path.name
        if kind == "audio":
            block["voice"] = True
        message |= {"type": kind, kind: block}
    elif b.get("template_button"):
        message |= {"type": "button", "button": {
            "text": b["template_button"], "payload": b.get("template_payload") or b["template_button"]}}
    else:
        message |= {"type": "text", "text": {"body": b["text"]}}
    if b.get("context_id"):
        message["context"] = {"from": DISPLAY_PHONE.replace(" ", "").lstrip("+"), "id": b["context_id"]}
    entry = _log("in", wa_id, message["type"], message)
    await _inbound([entry], {"contacts": [{"profile": {"name": name}, "wa_id": wa_id}], "messages": [message]})
    return {"ok": True}


@app.post("/phone/api/age")
async def age(wa_id: str, hours: float):
    """Move a conversation into the past, e.g. to close its 24-hour window."""
    for m in state["messages"]:
        if m["wa_id"] == wa_id:
            m["ts"] -= hours * 3600
    _persist()
    return {"ok": True}


@app.post("/phone/api/batch")
async def batch(request: Request):
    """Several texts from one person in a single webhook, as Meta sometimes batches them."""
    b = await request.json()
    msgs, entries = [], []
    for text in b["texts"]:
        m = {"from": b["wa_id"], "id": _wamid(), "timestamp": str(int(time.time())), "type": "text", "text": {"body": text}}
        entries.append(_log("in", b["wa_id"], "text", m))
        msgs.append(m)
    await _inbound(entries, {"contacts": [{"profile": {"name": b.get("name", b["wa_id"])}, "wa_id": b["wa_id"]}], "messages": msgs})
    return {"ok": True}


@app.get("/phone/api/thread")
async def thread(wa_id: str):
    out = []
    for m in state["messages"]:
        if m["wa_id"] != wa_id:
            continue
        item = dict(m)
        if m["kind"] == "template":
            t = m["body"].get("template") or {}
            item["template"] = state["templates"].get(f"{t.get('name')}:{(t.get('language') or {}).get('code')}")
        out.append(item)
    ids = {m.get("id") for m in out if m["dir"] == "in"}
    typing = any(t["message_id"] in ids and time.time() - t["ts"] < 25 for t in state.get("typing") or [])
    last_in = max((m["ts"] for m in out if m["dir"] == "in"), default=0)
    return {"messages": out, "window_open": _window_open(wa_id), "name": state["verified_name"],
            "typing": typing, "last_in": last_in, "now": time.time()}


@app.get("/phone/api/contacts")
async def contacts():
    last: dict[str, float] = {}
    for m in state["messages"]:
        last[m["wa_id"]] = max(last.get(m["wa_id"], 0), m["ts"])
    return {"contacts": sorted(last, key=last.get, reverse=True)}


@app.get("/phone/api/fixtures")
async def fixtures():
    if not FIXTURES or not os.path.isdir(FIXTURES):
        return {"files": []}
    return {"files": sorted(f for f in os.listdir(FIXTURES) if os.path.isfile(os.path.join(FIXTURES, f)))}


@app.get("/phone/api/state")
async def debug_state():
    return {k: v for k, v in state.items() if k not in ("messages", "app_secret", "templates")}


@app.get("/phone", response_class=HTMLResponse)
async def phone():
    return PHONE_HTML


PHONE_HTML = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Fake WhatsApp</title>
<style>
:root{--page:#05090b;--chat:#0b141a;--hd:#1f2c34;--hdt:#e9edef;--bin:#1f2c34;--mine:#005c4b;--tx:#e9edef;--mut:#8696a0;--acc:#00a884;--link:#53bdeb;--line:#2a3942;--field:#1f2c34;--sheet:#121b22;--sysb:#182229;--syst:#ffd279;--dateb:#182229;--datet:#8696a0;--quote:#0000002e;--shadow:0 1px .5px #0b141a40}
:root[data-theme=light]{--page:#d1d7db;--chat:#f4f4f5;--hd:#fff;--hdt:#111b21;--bin:#fff;--mine:#d9fdd3;--tx:#111b21;--mut:#667781;--acc:#1daa61;--link:#027eb5;--line:#e9edef;--field:#fff;--sheet:#fff;--sysb:#e4e7eb;--syst:#54656f;--dateb:#fff;--datet:#54656f;--quote:#0000000d;--shadow:0 1px .5px #0b141a21}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--tx);font:14.2px/19px -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.ic{fill:currentColor;flex:none;display:block}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;padding:0}
#bar{display:flex;gap:10px;align-items:center;padding:12px 16px;color:var(--mut);flex-wrap:wrap}
#bar b{color:var(--tx);font-size:16px;margin-right:auto}
#bar select,#bar button,.ctl select{background:var(--hd);color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
#phones{display:flex;gap:28px;padding:4px 16px 24px;flex-wrap:wrap;align-items:flex-start}
.wrap{display:flex;flex-direction:column;gap:8px}
.ctl{display:flex;gap:8px;align-items:center;color:var(--mut);font-size:13px}.ctl select{flex:1}.ctl .x{font-size:18px;padding:0 6px}
.phone{width:390px;height:844px;border-radius:52px;border:11px solid #000;box-shadow:0 0 0 2px #3b4a54,0 20px 50px #0008;background:var(--chat);position:relative;overflow:hidden;display:flex;flex-direction:column}
.sb{height:44px;flex:none;background:var(--hd);color:var(--hdt);display:flex;align-items:center;justify-content:space-between;padding:6px 26px 0;font-weight:600;font-size:15px}
.sb i{display:flex;gap:5px;align-items:center}.sb .bat{width:24px;height:11px;border:1.5px solid currentColor;border-radius:3px;padding:1px}.sb .bat::after{content:'';display:block;height:100%;width:70%;background:currentColor;border-radius:1px}
.hd{flex:none;background:var(--hd);color:var(--hdt);display:flex;align-items:center;gap:6px;padding:4px 8px 8px 4px;box-shadow:0 1px 0 var(--line);z-index:2}
.av{width:38px;height:38px;border-radius:50%;background:#2a6e5e;color:#fff;display:grid;place-items:center;font-weight:600;font-size:15px;flex:none}
.who{flex:1;min-width:0}.who .n{display:flex;align-items:center;gap:3px;font-weight:600;font-size:16px}.who .n span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.who .s{color:var(--mut);font-size:12.5px;line-height:16px}.who .s.ty{color:var(--acc)}
.vb{color:#25d366}
.hd .act{display:flex;gap:16px;padding:0 6px}
.chip{font-size:10.5px;line-height:14px;padding:1px 6px;border-radius:9px;margin-top:2px;display:inline-block;white-space:nowrap}
.chip.on{background:#00a88426;color:var(--acc)}.chip.off{background:#f1575726;color:#f15757}
.th{flex:1;overflow-y:auto;overflow-x:hidden;padding:6px 0 10px;scrollbar-width:thin}
.row{display:flex;padding:0 12px;margin-top:2px}.row.first{margin-top:10px}.row.in{justify-content:flex-end}.row.c{justify-content:center;margin:10px 0}
.sys{background:var(--sysb);color:var(--syst);font-size:12.5px;line-height:17px;text-align:center;border-radius:8px;padding:6px 10px;margin:4px 30px;box-shadow:var(--shadow)}
.day{background:var(--dateb);color:var(--datet);font-size:12.5px;border-radius:8px;padding:5px 12px;box-shadow:var(--shadow)}
.stk{max-width:82%;display:flex;flex-direction:column;gap:3px;min-width:0}.stk.w{width:82%}
.b{position:relative;padding:6px 7px 8px 9px;border-radius:8px;background:var(--bin);box-shadow:var(--shadow);white-space:pre-wrap;overflow-wrap:anywhere;min-width:0}
.in .b{background:var(--mine)}
.first.out .b.tl{border-top-left-radius:0}.first.in .b.tl{border-top-right-radius:0}
.first .b.tl::before{content:'';position:absolute;top:0;width:8px;height:13px}
.first.out .b.tl::before{left:-8px;background:var(--bin);clip-path:polygon(0 0,100% 0,100% 100%)}
.first.in .b.tl::before{right:-8px;background:var(--mine);clip-path:polygon(0 0,100% 0,0 100%)}
.b a{color:var(--link)}.b a.doc{color:var(--tx)}.b code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.t{float:right;display:inline-flex;align-items:center;gap:3px;margin:8px 0 -6px 12px;font-size:11px;line-height:15px;color:var(--mut);white-space:nowrap}
.tk{fill:currentColor}.tk.rd{color:#53bdeb}
.hh{font-weight:700;margin-bottom:2px}.ft{color:var(--mut);font-size:13px;margin-top:4px}
.qt{background:var(--quote);border-left:4px solid #06cf9c;border-radius:6px;padding:4px 8px;margin:-2px -4px 5px -6px;font-size:13px;line-height:17px;max-height:56px;overflow:hidden;white-space:normal}
.in .qt{border-color:#a5b337}.qt b{display:block;color:#06cf9c;font-size:12.5px}.in .qt b{color:#a5b337}.qt span{color:var(--mut)}
.qb{background:var(--bin);color:var(--link);box-shadow:var(--shadow);border-radius:8px;padding:9px 10px;display:flex;justify-content:center;align-items:center;gap:7px;font-weight:500;font-size:14.5px;text-decoration:none}
.qb:active,.ia:active{filter:brightness(1.25)}
.ia{border-top:1px solid var(--line);margin:8px -7px -8px -9px;padding:9px;display:flex;justify-content:center;align-items:center;gap:7px;color:var(--link);font-weight:500;font-size:14.5px;white-space:normal;clear:both;text-decoration:none}
.media{margin:-3px -4px 4px -6px;border-radius:6px;overflow:hidden;background:#0003;min-height:60px;display:grid;place-items:center;color:var(--mut)}
.media img{display:block;width:100%;aspect-ratio:4/3;object-fit:cover}.media.miss img{display:none}.media:not(.miss) .ph{display:none}
.ph{display:flex;flex-direction:column;align-items:center;gap:4px;padding:30px 0;font-size:12px}
.b.img{padding:3px 3px 6px;width:260px}.b.img .media{margin:0 0 4px}.b.img .cap{padding:0 5px}
.doc{display:flex;gap:10px;align-items:center;background:var(--quote);border-radius:6px;padding:10px;margin:-3px -4px 2px -6px;color:var(--tx);text-decoration:none;white-space:normal;min-width:220px}
.doc .fi{width:30px;height:38px;border-radius:3px;background:#e85b4f;color:#fff;font-size:8.5px;font-weight:700;display:grid;place-items:end center;padding-bottom:4px;flex:none}
.doc .nm{font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.doc small{color:var(--mut);font-size:11.5px}
.vn{display:flex;align-items:center;gap:8px;width:250px;padding:2px 0 0}
.vn .pl{color:var(--mut)}.vn .wv{flex:1;display:flex;align-items:center;gap:2px;height:28px}
.vn .wv i{flex:1;background:var(--mut);border-radius:2px;opacity:.6}.vn .wv i.p{background:#53bdeb;opacity:1}
.vn .va{width:42px;height:42px;border-radius:50%;background:#6b7c85;color:#fff;display:grid;place-items:center;position:relative;flex:none}
.vn .va .ic{position:absolute;right:-3px;bottom:-2px;color:#53bdeb}
.vd{position:absolute;bottom:5px;font-size:11px;color:var(--mut)}
.nfm{display:flex;gap:10px;align-items:center;background:var(--quote);border-radius:6px;padding:9px 10px;margin:-3px -4px 2px -6px;white-space:normal;min-width:210px}
.nfm .fic{width:36px;height:36px;border-radius:8px;background:#00a88433;color:var(--acc);display:grid;place-items:center;flex:none}
.nfm small{display:block;color:var(--mut);font-size:12px}
.ans{white-space:normal;margin-top:6px;font-size:13px}.ans div{padding:3px 0;border-bottom:1px solid var(--line)}.ans b{display:block;color:var(--mut);font-weight:500;font-size:11.5px}
.rx{position:absolute;bottom:-14px;background:var(--bin);border-radius:12px;padding:0 5px;font-size:14px;box-shadow:0 1px 3px #0005;border:2px solid var(--chat)}.out .rx{left:8px}.in .rx{right:8px}.row.hasrx{margin-bottom:14px}
.sys .lk{display:inline;vertical-align:-1px;margin-right:4px}
.unk{color:var(--mut);font-style:italic}
.cmp{flex:none;display:flex;align-items:flex-end;gap:6px;padding:6px 6px 26px;position:relative}
.fld{flex:1;display:flex;align-items:flex-end;background:var(--field);border-radius:24px;min-height:48px;padding:0 6px 0 16px;box-shadow:var(--shadow)}
.fld textarea{flex:1;border:0;outline:0;background:none;color:var(--tx);font:inherit;font-size:16px;line-height:21px;resize:none;padding:13px 0;max-height:110px}
.fld textarea::placeholder{color:var(--mut)}
.fld button{color:var(--mut);padding:12px 6px}
.go{width:48px;height:48px;border-radius:50%;background:var(--acc);color:#fff;display:grid;place-items:center;flex:none}
.go .snd{display:none}.cmp.typed .go .snd{display:block}.cmp.typed .go .mic{display:none}
.menu{position:absolute;left:8px;right:8px;bottom:84px;background:var(--sheet);border-radius:18px;padding:14px;box-shadow:0 4px 24px #0007;display:none;z-index:5}
.menu.open{display:block}
.menu .tiles{display:flex;gap:22px;justify-content:center;margin-bottom:8px}
.menu .tile{display:flex;flex-direction:column;align-items:center;gap:6px;font-size:12.5px;color:var(--mut)}
.menu .tile i{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;color:#fff}
.menu h4{margin:8px 4px 4px;font-size:12px;color:var(--mut);font-weight:500}
.menu .fx{display:block;width:100%;text-align:left;padding:8px 6px;border-radius:8px;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.menu .fx:hover{background:var(--line)}
.menu .none{color:var(--mut);font-size:12.5px;padding:4px 6px}
.toast{position:absolute;left:50%;bottom:96px;transform:translateX(-50%);background:#323739;color:#fff;padding:7px 14px;border-radius:18px;font-size:13px;z-index:9;opacity:0;transition:opacity .2s;pointer-events:none;white-space:nowrap}.toast.show{opacity:1}
.scrim{position:absolute;inset:0;background:#0009;z-index:6;display:none;align-items:flex-end}.scrim.open{display:flex}
.ls{width:100%;max-height:78%;background:var(--sheet);border-radius:18px 18px 0 0;display:flex;flex-direction:column;animation:up .2s ease-out;position:relative}
@keyframes up{from{transform:translateY(40%);opacity:.4}}
.grab{width:36px;height:4px;border-radius:2px;background:var(--mut);opacity:.5;margin:8px auto 0}
.ls .top{display:flex;align-items:center;gap:12px;padding:10px 16px 12px;font-weight:600;font-size:17px;color:var(--tx)}.ls .top span{flex:1;text-align:center;margin-right:36px}
.ls .body{overflow-y:auto;padding:0 0 90px}
.ls .sec{color:var(--mut);font-size:13.5px;padding:14px 20px 4px}
.ls label{display:flex;align-items:center;gap:12px;padding:11px 20px;cursor:pointer}.ls label:hover{background:var(--line)}
.ls label span{flex:1}.ls label small{display:block;color:var(--mut);font-size:13px;line-height:17px;margin-top:1px}
.rad{appearance:none;width:20px;height:20px;border:2px solid var(--mut);border-radius:50%;margin:0;flex:none;display:grid;place-items:center;cursor:pointer}
.rad:checked{border-color:var(--acc)}.rad:checked::after{content:'';width:10px;height:10px;border-radius:50%;background:var(--acc)}
.fab{position:absolute;right:18px;bottom:22px;width:56px;height:56px;border-radius:50%;background:var(--acc);color:#fff;display:grid;place-items:center;box-shadow:0 3px 10px #0006}
.fab:disabled{background:var(--line);color:var(--mut);box-shadow:none;cursor:default}
.fl{position:absolute;inset:44px 0 0;background:var(--sheet);z-index:7;display:none;flex-direction:column;border-radius:14px 14px 0 0;animation:up .22s ease-out}.fl.open{display:flex}
.fl .top{display:flex;align-items:center;gap:14px;padding:14px 14px 12px;border-bottom:1px solid var(--line);color:var(--tx)}.fl .top b{flex:1;font-size:17px;font-weight:600}
.fl form{flex:1;overflow-y:auto;padding:6px 18px 20px;display:flex;flex-direction:column;gap:4px}
.fl h2{font-size:22px;line-height:28px;margin:14px 0 4px}.fl h3{font-size:17px;margin:12px 0 2px}.fl p{margin:4px 0;white-space:pre-wrap}.fl p.cap{color:var(--mut);font-size:12.5px;line-height:16px}
.fi{position:relative;display:block;margin:12px 0 0}
.fi input,.fi textarea,.fi select{width:100%;border:1px solid var(--mut);border-radius:8px;background:none;color:var(--tx);font:inherit;font-size:16px;padding:16px 14px 8px;outline:0;appearance:none;color-scheme:dark}
:root[data-theme=light] .fi input,:root[data-theme=light] .fi select{color-scheme:light}
.fi select option{background:var(--sheet)}
.fi textarea{min-height:96px;resize:none}
.fi input:focus,.fi textarea:focus,.fi select:focus{border:2px solid var(--acc);padding:15px 13px 7px}
.fi>span{position:absolute;left:14px;top:15px;color:var(--mut);font-size:16px;pointer-events:none;transition:.15s;background:var(--sheet);padding:0 3px;margin-left:-3px}
.fi input:focus+span,.fi input:not(:placeholder-shown)+span,.fi textarea:focus+span,.fi textarea:not(:placeholder-shown)+span,.fi.up>span{top:-8px;font-size:12px;line-height:14px}
.fi input:focus+span,.fi textarea:focus+span{color:var(--acc)}
.fi .chev{position:absolute;right:12px;top:16px;color:var(--mut);pointer-events:none}
.help{color:var(--mut);font-size:12px;line-height:16px;margin:4px 14px 0}
.req{color:#f15c6d}
.rg{border:0;padding:0;margin:14px 0 0}.rg legend{font-size:16px;font-weight:500;padding:0}.rg .d{color:var(--mut);font-size:13.5px;margin:2px 0 4px}
.rg label{display:flex;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line);cursor:pointer}.rg label span{flex:1}.rg label small{display:block;color:var(--mut);font-size:13px;line-height:17px}
.rg label.off{opacity:.45;pointer-events:none}
.chk{appearance:none;width:20px;height:20px;border:2px solid var(--mut);border-radius:3px;margin:0;flex:none;display:grid;place-items:center;cursor:pointer}
.chk:checked{background:var(--acc);border-color:var(--acc)}.chk:checked::after{content:'';width:5px;height:10px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg) translate(-1px,-1px)}
.optin{display:flex;gap:12px;align-items:flex-start;margin:16px 0 0;cursor:pointer}.optin .chk{margin-top:1px}
.fl img{max-width:100%;border-radius:8px;margin:8px 0}
.elink{color:var(--link);margin:10px 0;display:block}
.ff{flex:none;padding:10px 18px 26px;border-top:1px solid var(--line);text-align:center}
.ff button{width:100%;height:46px;border-radius:24px;background:var(--acc);color:#fff;font-weight:600;font-size:15.5px}
.ff button:disabled{background:var(--line);color:var(--mut);cursor:default}
.ff small{display:block;color:var(--mut);font-size:12px;margin-top:10px}.ff small a{color:var(--link)}
</style>
<div id=bar><b>Fake WhatsApp</b><select id=add><option value="">+ open a phone…</option></select><button id=theme>Light theme</button></div>
<div id=phones></div>
<input type=file id=pick hidden>
<script>
const KNOWN={'40712000001':'Ion Pacient','40712000002':'Dr. Radu Pretorian'};
const P={back:'M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z',
video:'M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z',
call:'M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z',
more:'M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z',
reply:'M10 9V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z',
list:'M4 10.5c-.83 0-1.5.67-1.5 1.5s.67 1.5 1.5 1.5 1.5-.67 1.5-1.5-.67-1.5-1.5-1.5zm0-6c-.83 0-1.5.67-1.5 1.5S3.17 7.5 4 7.5 5.5 6.83 5.5 6 4.83 4.5 4 4.5zm0 12c-.83 0-1.5.68-1.5 1.5s.68 1.5 1.5 1.5 1.5-.68 1.5-1.5-.67-1.5-1.5-1.5zM7 19h14v-2H7v2zm0-6h14v-2H7v2zm0-8v2h14V5H7z',
open:'M19 19H5V5h7V3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z',
clip:'M16.5 6v11.5c0 2.21-1.79 4-4 4s-4-1.79-4-4V5c0-1.38 1.12-2.5 2.5-2.5s2.5 1.12 2.5 2.5v10.5c0 .55-.45 1-1 1s-1-.45-1-1V6H10v9.5c0 1.38 1.12 2.5 2.5 2.5s2.5-1.12 2.5-2.5V5c0-2.21-1.79-4-4-4S7 2.79 7 5v12.5c0 3.04 2.46 5.5 5.5 5.5s5.5-2.46 5.5-5.5V6h-1.5z',
mic:'M12 14c1.66 0 2.99-1.34 2.99-3L15 5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.48 6-3.3 6-6.72h-1.7z',
send:'M2.01 21 23 12 2.01 3 2 10l15 2-15 2z',play:'M8 5v14l11-7z',pause:'M6 19h4V5H6v14zm8-14v14h4V5h-4z',
doc:'M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z',
close:'M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z',
cam:'M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4zM9 2 7.17 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2h-3.17L15 2H9zm3 15c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5z',
img:'M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z',
verified:'M23 12l-2.44-2.79.34-3.69-3.61-.82-1.89-3.2L12 2.96 8.6 1.5 6.71 4.69 3.1 5.5l.34 3.7L1 12l2.44 2.79-.34 3.7 3.61.82L8.6 22.5l3.4-1.47 3.4 1.46 1.89-3.19 3.61-.82-.34-3.69L23 12zm-12.91 4.72-3.8-3.81 1.48-1.48 2.32 2.33 5.85-5.87 1.48 1.48-7.33 7.35z',
form:'M19 3h-4.18C14.4 1.84 13.3 1 12 1c-1.3 0-2.4.84-2.82 2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-7 0c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zm2 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z',
chev:'M7.41 8.59 12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z',
cal:'M19 4h-1V2h-2v2H8V2H6v2H5c-1.11 0-1.99.9-1.99 2L3 20a2 2 0 0 0 2 2h14c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 16H5V10h14v10z',
lock:'M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1s3.1 1.39 3.1 3.1v2z',
person:'M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z'};
const ic=(n,s=24,c='')=>`<svg class="ic ${c}" viewBox="0 0 24 24" width=${s} height=${s}><path d="${P[n]}"/></svg>`;
const TICK1='<svg class=tk viewBox="0 0 16 11" width=16 height=11><path d="M11.1.6 4.6 7.1 1.9 4.4.9 5.5l3.7 3.7 7.5-7.5z"/></svg>';
const TICK2=c=>`<svg class="tk ${c}" viewBox="0 0 16 11" width=16 height=11><path d="M11.1.6 4.6 7.1 1.9 4.4.9 5.5l3.7 3.7 7.5-7.5zM15.1.6 8.6 7.1l-.8-.8-1 1.1 1.8 1.8 7.5-7.5z"/></svg>`;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const J=o=>esc(JSON.stringify(o??{}));
const hm=ts=>new Date(ts*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
const initials=s=>String(s).replace(/[^\p{L}\p{N} ]/gu,' ').split(/\s+/).filter(w=>w&&!/^(dr|mr|mrs)$/i.test(w)).slice(0,2).map(w=>w[0].toUpperCase()).join('')||'?';
function store(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}}

/* WhatsApp formatting: ```mono```, links, *bold*, _italic_, ~strike~ */
function wrap(s,ch,tag){const c='\\'+ch;return s.replace(new RegExp(`(^|[\\s([{>«„"'])${c}(?=\\S)([^${c}\\n]*?\\S)${c}(?=$|[\\s.,!?;:)\\]}<»”"'])`,'g'),`$1<${tag}>$2</${tag}>`)}
const style=s=>wrap(wrap(wrap(s,'*','b'),'_','i'),'~','s');
const inline=t=>t.split(/(https?:\/\/[^\s<]*[^\s<.,;:!?)\]'"»])/).map((p,i)=>i%2?`<a href="${esc(p)}" target=_blank rel=noopener>${esc(p)}</a>`:style(esc(p))).join('');
const fmt=t=>String(t??'').split(/```([\s\S]*?)```/).map((p,i)=>i%2?`<code>${esc(p)}</code>`:inline(p)).join('');

function dayLabel(ts){const d=new Date(ts*1000),n=new Date(),k=x=>x.toDateString();
  if(k(d)===k(n))return 'Today';const y=new Date(n);y.setDate(n.getDate()-1);if(k(d)===k(y))return 'Yesterday';
  if(n-d<6*864e5)return d.toLocaleDateString('en-GB',{weekday:'long'});return d.toLocaleDateString('en-GB',{day:'numeric',month:'long',year:'numeric'})}

function tpl(m){
  const t=m.template||{},c=t.components||[],sent=m.body.template?.components||[];
  const params=type=>(sent.find(x=>String(x.type).toLowerCase()===type)?.parameters)||[];
  const fill=(s,ps)=>String(s||'').replace(/\{\{\s*(\w+)\s*\}\}/g,(a,k)=>{const p=/^\d+$/.test(k)?ps[k-1]:ps.find(x=>x.parameter_name===k);return p?(p.text??p.currency?.fallback_value??p.date_time?.fallback_value??a):a});
  const g=x=>c.find(y=>y.type===x),H=g('HEADER'),B=g('BODY'),F=g('FOOTER');
  const bp=i=>sent.find(x=>String(x.type).toLowerCase()==='button'&&String(x.index)===String(i));
  return {H,hp:params('header')[0],header:H&&(H.format||'TEXT')==='TEXT'?fill(H.text,params('header')):'',
    body:B?fill(B.text,params('body')):`[template ${m.body.template?.name}]`,footer:F?.text||'',buttons:g('BUTTONS')?.buttons||[],bp}}

function snippet(m){const b=m.body||{},i=b.interactive||{};
  switch(m.kind){case 'text':return b.text?.body;case 'template':return tpl(m).body;case 'image':return '📷 '+(b.image?.caption||'Photo');
    case 'audio':return '🎤 Voice message';case 'document':return '📄 '+(b.document?.filename||'Document');case 'video':return '🎥 Video';case 'button':return b.button?.text;
    case 'interactive':return i.body?.text||i.button_reply?.title||i.list_reply?.title||(i.nfm_reply?'📋 Form':'');default:return m.kind}}

function mediaSrc(o){return o?.link||(o?.id?'/media-bytes/'+encodeURIComponent(o.id):'')}
function imgBlock(o){return `<div class=media><img src="${esc(mediaSrc(o))}" loading=lazy onerror="this.parentNode.classList.add('miss')"><div class=ph>${ic('img',40)}<span>image not kept by this fake</span></div></div>`}
function docBlock(o){const n=o?.filename||'Document',ext=(n.split('.').pop()||'').slice(0,4).toUpperCase();
  return `<a class=doc href="${esc(mediaSrc(o))}" target=_blank><span class=fi>${esc(ext)}</span><span style="min-width:0"><div class=nm>${esc(n)}</div><small>${esc(ext)} · ${esc(o?.mime_type||'')}</small></span></a>`}
const DUR={};
function voice(m,o,mine){const id=m.id,src=mediaSrc(o);let h=0;for(const ch of id)h=(h*31+ch.charCodeAt(0))>>>0;
  const bars=Array.from({length:34},(_,i)=>{h=(h*1103515245+12345)>>>0;return `<i style="height:${4+(h>>>16)%22}px"></i>`}).join('');
  const d=DUR[id],dur=d?`${Math.floor(d/60)}:${String(Math.round(d%60)).padStart(2,'0')}`:'0:00';
  const av=`<span class=va>${ic('person',26)}${ic('mic',16)}</span>`;
  return `<div class=vn data-voice="${esc(id)}" data-src="${esc(src)}">${mine?'':av}<button class=pl data-k=play>${ic('play',30)}</button><span class=wv>${bars}</span>${mine?av:''}</div><span class=vd style="left:${mine?46:60}px">${dur}</span>`}

const ticks=(m,later)=>m.status==='read'?TICK2('rd'):(m.status==='delivered'||later)?TICK2(''):TICK1;
const flowBtn=(cls,text,a)=>`<a class=${cls} data-k=flow data-a="${J(a)}">${ic('form',18)}${esc(text)}</a>`;

function bubble(p,m,ctx){
  const b=m.body||{},mine=m.dir==='in',time=`<span class=t>${hm(m.ts)}${mine?ticks(m,ctx.later):''}</span>`;
  let quote='';const ref=mine?b.context?.id:b.context?.message_id;
  if(ref&&ctx.byId[ref]&&!(mine&&b.interactive?.nfm_reply)){const r=ctx.byId[ref];quote=`<div class=qt><b>${r.dir==='in'?'You':esc(ctx.name)}</b><span>${esc(String(snippet(r)||'').slice(0,120))}</span></div>`}
  let inner='',cls='b tl',below='',wide=false;
  if(m.kind==='text')inner=fmt(b.text?.body);
  else if(m.kind==='button')inner=fmt(b.button?.text);
  else if(m.kind==='image'){cls+=' img';inner=imgBlock(b.image)+(b.image?.caption?`<div class=cap>${fmt(b.image.caption)}</div>`:'')}
  else if(m.kind==='document')inner=docBlock(b.document)+(b.document?.caption?fmt(b.document.caption):'');
  else if(m.kind==='audio')inner=voice(m,b.audio,mine);
  else if(m.kind==='video')inner=`<div class=media><div class=ph>${ic('video',40)}<span>video</span></div></div>`+fmt(b.video?.caption||'');
  else if(m.kind==='sticker')inner=imgBlock(b.sticker);
  else if(m.kind==='location')inner=`📍 ${esc(b.location?.name||'')} ${esc(b.location?.address||'')}`;
  else if(m.kind==='template'){const T=tpl(m);wide=T.buttons.length>0;
    if(T.H&&T.H.format&&T.H.format!=='TEXT'){const o=T.hp?.[String(T.H.format).toLowerCase()];inner+=T.H.format==='IMAGE'?imgBlock(o):T.H.format==='DOCUMENT'?docBlock(o):''}
    if(T.header)inner+=`<div class=hh>${fmt(T.header)}</div>`;inner+=fmt(T.body);if(T.footer)inner+=`<div class=ft>${esc(T.footer)}</div>`;
    T.buttons.forEach((x,i)=>{const bp=T.bp(i);
      if(x.type==='FLOW'){const a=bp?.parameters?.[0]?.action||{};below+=flowBtn('qb',x.text,{flow:x.flow_id,token:a.flow_token||'unused',screen:x.navigate_screen,data:a.flow_action_data||{},ctx:m.id,cta:x.text})}
      else if(x.type==='URL')below+=`<a class=qb href="${esc(String(x.url||'').replace(/\{\{1\}\}/,bp?.parameters?.[0]?.text||''))}" target=_blank>${ic('open',18)}${esc(x.text)}</a>`;
      else if(x.type==='PHONE_NUMBER')below+=`<a class=qb href="tel:${esc(x.phone_number)}">${ic('call',18)}${esc(x.text)}</a>`;
      else below+=`<button class=qb data-k=tb data-v="${esc(x.text)}" data-pl="${esc(bp?.parameters?.[0]?.payload||x.text)}" data-ctx="${esc(m.id)}">${ic('reply',18)}${esc(x.text)}</button>`})}
  else if(m.kind==='interactive'){const i=b.interactive||{};
    if(mine){
      if(i.nfm_reply){let ans={};try{ans=JSON.parse(i.nfm_reply.response_json)}catch(e){}delete ans.flow_token;
        const src=ctx.byId[b.context?.id],cta=src?.body?.interactive?.action?.parameters?.flow_cta||(src?.kind==='template'?tpl(src).buttons.find(x=>x.type==='FLOW')?.text:'')||'Form';
        const open=p.openAns.has(m.id);
        inner=`<div class=nfm><span class=fic>${ic('form',20)}</span><span><b>${esc(cta)}</b><small>${esc(i.nfm_reply.body||'Sent')} · Form submitted</small></span></div>`
          +(open?`<div class=ans>${Object.entries(ans).map(([k,v])=>`<div><b>${esc(k)}</b>${esc(Array.isArray(v)?v.join(', '):v)}</div>`).join('')}</div>`:'')
          +time+`<a class=ia data-k=ans data-id="${esc(m.id)}">${open?'Hide response':'View response'}</a>`;return {h:quote?`<div class="${cls}">${quote}${inner}</div>`:`<div class="${cls}">${inner}</div>`,wide:false}}
      const r=i.button_reply||i.list_reply||{};inner=fmt(r.title)+(r.description?`<div class=ft>${esc(r.description)}</div>`:'');
    }else{
      const h=i.header||{};
      if(h.type==='text'&&h.text)inner+=`<div class=hh>${fmt(h.text)}</div>`;else if(h.type==='image')inner+=imgBlock(h.image);else if(h.type==='document')inner+=docBlock(h.document);
      inner+=fmt(i.body?.text);if(i.footer?.text)inner+=`<div class=ft>${esc(i.footer.text)}</div>`;
      if(i.type==='button'){wide=true;for(const x of i.action?.buttons||[])below+=`<button class=qb data-k=br data-id="${esc(x.reply?.id)}" data-v="${esc(x.reply?.title)}" data-ctx="${esc(m.id)}">${ic('reply',18)}${esc(x.reply?.title)}</button>`}
      if(i.type==='list'){wide=true;return {h:`<div class="${cls}">${quote}${inner}${time}<a class=ia data-k=list data-id="${esc(m.id)}">${ic('list',18)}${esc(i.action?.button||'Options')}</a></div>`,wide}}
      if(i.type==='flow'){wide=true;const f=i.action?.parameters||{};
        return {h:`<div class="${cls}">${quote}${inner}${time}${flowBtn('ia',f.flow_cta||'Open',{flow:f.flow_id,token:f.flow_token||'unused',screen:f.flow_action_payload?.screen,data:f.flow_action_payload?.data||{},ctx:m.id,cta:f.flow_cta})}</div>`,wide}}
      if(i.type==='cta_url'){wide=true;const f=i.action?.parameters||{};
        return {h:`<div class="${cls}">${quote}${inner}${time}<a class=ia href="${esc(f.url)}" target=_blank>${ic('open',18)}${esc(f.display_text)}</a></div>`,wide}}
    }}
  else inner=`<span class=unk>Unsupported message: ${esc(m.kind)}</span>`;
  const rx=ctx.rx[m.id]?`<span class=rx>${esc(ctx.rx[m.id])}</span>`:'';
  const bub=`<div class="${cls}">${quote}${inner}${time}${rx}</div>`;
  return {h:below?bub+below:bub,wide,rx:!!rx}}

function items(p,r){
  const out=[{k:'e2e',c:'row c',h:`<div class=sys>${ic('lock',12,'lk')}This business uses a secure service from Meta to manage this chat. Tap to learn more.</div>`}];
  const ms=r.messages,byId={},rx={};for(const m of ms){if(m.id)byId[m.id]=m;if(m.kind==='reaction'&&m.dir==='out')rx[m.body.reaction?.message_id]=m.body.reaction?.emoji}
  let lastOut=0;for(const m of ms)if(m.dir==='out')lastOut=Math.max(lastOut,m.ts);
  let day='',prev='';
  ms.forEach((m,idx)=>{if(m.kind==='reaction')return;
    const d=new Date(m.ts*1000).toDateString();if(d!==day){day=d;prev='';out.push({k:'d'+d,c:'row c',h:`<span class=day>${dayLabel(m.ts)}</span>`})}
    const first=m.dir!==prev;prev=m.dir;
    const B=bubble(p,m,{byId,rx,name:r.name,later:lastOut>m.ts});
    out.push({k:m.id||'i'+idx,c:`row ${m.dir==='in'?'in':'out'}${first?' first':''}${B.rx?' hasrx':''}`,h:`<div class="stk${B.wide?' w':''}">${B.h}</div>`})});
  return out}

function reconcile(list,its,cache){
  const keep=new Set();
  its.forEach((it,i)=>{let el=cache.get(it.k);
    if(!el||el._h!==it.h||el.className!==it.c){const n=document.createElement('div');n.className=it.c;n.innerHTML=it.h;n._h=it.h;if(el)el.replaceWith(n);el=n;cache.set(it.k,n)}
    keep.add(it.k);if(list.children[i]!==el)list.insertBefore(el,list.children[i]||null)});
  for(const[k,el]of cache)if(!keep.has(k)){el.remove();cache.delete(k)}}

/* ---------- phones ---------- */
let phones=[],CONTACTS=[],FIXTURES=[];
function shell(p){
  const w=document.createElement('div');w.className='wrap';
  w.innerHTML=`<div class=ctl><select class=pick></select><button class=x title="Close this phone">×</button></div>
  <div class=phone><div class=sb><span class=clk></span><i><svg width=17 height=11 viewBox="0 0 17 11" class=ic><rect x=0 y=7 width=3 height=4 rx=1 /><rect x=4.5 y=5 width=3 height=6 rx=1 /><rect x=9 y=2.5 width=3 height=8.5 rx=1 /><rect x=13.5 y=0 width=3 height=11 rx=1 /></svg><svg width=16 height=12 viewBox="0 0 24 18" class=ic><path d="M12 18 0 5.5A17 17 0 0 1 24 5.5z"/></svg><span class=bat></span></i></div>
  <div class=hd><button title=Back>${ic('back')}</button><span class=av></span><div class=who><div class=n><span class=nm></span>${ic('verified',16,'vb')}</div><div class=s></div><span class=chip></span></div><span class=act>${ic('video')}${ic('call',22)}${ic('more')}</span></div>
  <div class=th><div class=list></div></div>
  <form class=cmp><div class=menu></div><div class=fld><textarea rows=1 placeholder=Message></textarea><button type=button class=att title=Attach>${ic('clip')}</button><button type=button class=cam title="Photo">${ic('cam')}</button></div><button type=submit class=go title="Send / voice note">${ic('send',22,'snd')}${ic('mic',24,'mic')}</button><span class=toast></span></form>
  <div class=scrim></div><div class=fl></div></div>`;
  p.el=w;p.cache=new Map();p.openAns=new Set();p.first=true;
  const ta=w.querySelector('textarea'),cmp=w.querySelector('.cmp');
  const grow=()=>{ta.style.height='auto';ta.style.height=ta.scrollHeight+'px';cmp.classList.toggle('typed',!!ta.value.trim())};
  ta.addEventListener('input',grow);
  ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(ta.value.trim())cmp.requestSubmit()}});
  cmp.onsubmit=e=>{e.preventDefault();const v=ta.value.trim();
    if(!v){const vf=FIXTURES.find(f=>/\.(ogg|opus|mp3|m4a|wav|aac|amr)$/i.test(f));if(!vf)return toast(p,'No voice note in --fixtures');return send(p,{fixture:vf,kind:'audio'})}
    ta.value='';grow();send(p,{text:v})};
  w.querySelector('.att').onclick=()=>{fillMenu(p);w.querySelector('.menu').classList.toggle('open')};
  w.querySelector('.cam').onclick=()=>pickFile(p,'image/*');
  w.querySelector('.x').onclick=()=>{phones=phones.filter(x=>x!==p);w.remove();save()};
  w.querySelector('.pick').onchange=e=>{p.wa=e.target.value;p.name=KNOWN[p.wa]||p.wa;p.cache.clear();w.querySelector('.list').innerHTML='';p.first=true;opts(p);save();poll()};
  w.addEventListener('click',e=>onClick(p,e));
  w.querySelector('.th').addEventListener('click',()=>w.querySelector('.menu').classList.remove('open'));
  opts(p);return w}
function opts(p){const s=p.el.querySelector('.pick'),list=[...new Set([p.wa,...CONTACTS])];
  const want=list.map(w=>`<option value="${esc(w)}"${w===p.wa?' selected':''}>+${esc(w)}${KNOWN[w]?' · '+esc(KNOWN[w]):''}</option>`).join('');
  if(s._h!==want){s.innerHTML=want;s._h=want}}
function fillMenu(p){const m=p.el.querySelector('.menu');
  m.innerHTML=`<div class=tiles><button type=button class=tile data-k=up data-acc="*/*"><i style="background:#7f66ff">${ic('doc')}</i>Document</button><button type=button class=tile data-k=up data-acc="image/*"><i style="background:#007bfc">${ic('img')}</i>Gallery</button><button type=button class=tile data-k=up data-acc="audio/*"><i style="background:#ff2e74">${ic('mic')}</i>Audio</button></div>
  <h4>Test files (--fixtures)</h4>${FIXTURES.length?FIXTURES.map(f=>`<button type=button class=fx data-k=fx data-v="${esc(f)}">${/\.(png|jpe?g|webp|gif)$/i.test(f)?'🖼️':/\.(ogg|opus|mp3|m4a|wav)$/i.test(f)?'🎤':'📄'} ${esc(f)}</button>`).join(''):'<div class=none>Start the fake with --fixtures DIR to list files here.</div>'}`}
function toast(p,t){const el=p.el.querySelector('.toast');el.textContent=t;el.classList.add('show');clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove('show'),2200)}
function pickFile(p,accept){const f=document.getElementById('pick');f.accept=accept;f.value='';
  f.onchange=()=>{const file=f.files[0];if(!file)return;const rd=new FileReader();
    rd.onload=()=>send(p,{media_b64:String(rd.result).split(',')[1],filename:file.name,mime:file.type||undefined,kind:file.type.startsWith('audio/')?'audio':undefined});rd.readAsDataURL(file)};f.click()}

async function send(p,extra){p.el.querySelector('.menu').classList.remove('open');p.stick=true;
  const r=await fetch('/phone/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wa_id:p.wa,name:p.name,...extra})});
  if(!r.ok)toast(p,'Send failed: '+r.status);poll()}

const AUD={};
function onClick(p,e){const t=e.target.closest('[data-k]');if(!t)return;const k=t.dataset.k;
  if(['tb','br','fx','up','list','flow','ans','play','lsx','lsend'].includes(k))e.preventDefault();
  if(k==='tb')send(p,{template_button:t.dataset.v,template_payload:t.dataset.pl,context_id:t.dataset.ctx});
  if(k==='br')send(p,{button_reply:{id:t.dataset.id,title:t.dataset.v},context_id:t.dataset.ctx});
  if(k==='fx'){const f=t.dataset.v;send(p,{fixture:f,kind:/\.(ogg|opus|mp3|m4a|wav|aac|amr)$/i.test(f)?'audio':undefined})}
  if(k==='up')pickFile(p,t.dataset.acc);
  if(k==='list')openList(p,t.dataset.id);
  if(k==='flow')openFlow(p,JSON.parse(t.dataset.a));
  if(k==='ans'){const id=t.dataset.id;p.openAns.has(id)?p.openAns.delete(id):p.openAns.add(id);render(p)}
  if(k==='play'){const v=t.closest('[data-voice]'),id=v.dataset.voice;let a=AUD[id];
    if(!a){a=AUD[id]=new Audio(v.dataset.src);a.onerror=()=>toast(p,'Audio not kept by this fake');
      a.ontimeupdate=a.onended=()=>{const el=p.el.querySelector(`[data-voice="${CSS.escape(id)}"]`);if(!el)return;const bars=el.querySelectorAll('.wv i'),n=Math.round(bars.length*(a.ended?0:a.currentTime/(isFinite(a.duration)?a.duration:DUR[id]||1)));bars.forEach((b,i)=>b.classList.toggle('p',i<n));el.querySelector('.pl').innerHTML=ic(a.paused?'play':'pause',30)}}
    a.paused?a.play().catch(()=>{}):a.pause();t.innerHTML=ic(a.paused?'play':'pause',30)}}

/* ---------- interactive list sheet ---------- */
function openList(p,id){const m=p.data.messages.find(x=>x.id===id),i=m.body.interactive,s=p.el.querySelector('.scrim');
  s.innerHTML=`<div class=ls><div class=grab></div><div class=top><button data-k=lsx>${ic('close')}</button><span>${esc(i.action.button||'')}</span></div><div class=body>${(i.action.sections||[]).map((sec,si)=>
    (sec.title?`<div class=sec>${esc(sec.title)}</div>`:'')+(sec.rows||[]).map(r=>`<label><span>${esc(r.title)}${r.description?`<small>${esc(r.description)}</small>`:''}</span><input class=rad type=radio name=lr value="${esc(r.id)}" data-t="${esc(r.title)}" data-d="${esc(r.description||'')}"></label>`).join('')).join('')}</div>
    <button class=fab data-k=lsend disabled title=Send>${ic('send',24)}</button></div>`;
  s.classList.add('open');
  s.onchange=()=>s.querySelector('.fab').disabled=!s.querySelector('input:checked');
  s.onclick=e=>{const k=e.target.closest('[data-k]')?.dataset.k;
    if(e.target===s||k==='lsx')s.classList.remove('open');
    if(k==='lsend'){const c=s.querySelector('input:checked');if(!c)return;s.classList.remove('open');
      send(p,{list_reply:{id:c.value,title:c.dataset.t,...(c.dataset.d?{description:c.dataset.d}:{})},context_id:id})}}}

/* ---------- WhatsApp Flows ---------- */
function expr(x,values,data){if(typeof x!=='string')return x;const r=/^\$\{(.+)\}$/.exec(x);if(!r)return x;const path=r[1].split('.');
  if(path[0]==='form'||(path[0]==='screen'&&path[2]==='form'))return values[path[path.length-1]];if(path[0]==='data')return data[path[path.length-1]]??values[path[path.length-1]];return undefined}
function kids(n,out){if(Array.isArray(n))n.forEach(c=>kids(c,out));else if(n&&typeof n==='object'){
  if(n.type==='If'){kids(n.then||[],out);return out}if(n.type==='Form'){const init=n['init-values'];out.push({type:'__form',init});kids(n.children||[],out);return out}
  out.push(n);if(n.type!=='Footer')kids(n.children||[],out)}return out}
function comp(c,data,init){const v=expr(init[c.name]??c['init-value'],{},data)??'',req=c.required?' <span class=req>*</span>':'',rq=c.required?' required':'',nm=`name="${esc(c.name)}"`;
  const help=c['helper-text']?`<div class=help>${esc(c['helper-text'])}</div>`:'',opts=expr(c['data-source'],{},data)||[];
  const row=(o,type,on)=>`<label${o.enabled===false?' class=off':''}><span>${esc(o.title)}${o.description?`<small>${esc(o.description)}</small>`:''}</span><input class=${type==='radio'?'rad':'chk'} type=${type} ${nm} value="${esc(o.id)}"${on?' checked':''}${type==='checkbox'?' data-multi':''}></label>`;
  switch(c.type){
    case 'TextHeading':return `<h2>${fmt(c.text)}</h2>`;case 'TextSubheading':return `<h3>${fmt(c.text)}</h3>`;
    case 'TextBody':return `<p>${fmt(c.text)}</p>`;case 'TextCaption':return `<p class=cap>${fmt(c.text)}</p>`;
    case 'RichText':return `<p>${fmt([].concat(c.text||'').join('\n'))}</p>`;
    case 'TextInput':{const t={number:'number',email:'email',phone:'tel',password:'password',passcode:'password'}[c['input-type']]||'text';
      return `<label class=fi><input type=${t} ${nm} placeholder=" " value="${esc(v)}"${rq}${t==='number'?' inputmode=decimal step=any':''}><span>${esc(c.label)}${req}</span></label>${help}`}
    case 'TextArea':return `<label class=fi><textarea ${nm} placeholder=" "${rq}>${esc(v)}</textarea><span>${esc(c.label)}${req}</span></label>${help}`;
    case 'DatePicker':case 'CalendarPicker':return `<label class="fi up"><input type=date ${nm} value="${esc(v)}"${rq}><span>${esc(c.label)}${req}</span></label>${help}`;
    case 'Dropdown':return `<label class="fi up"><select ${nm}${rq}><option value="">Select</option>${opts.map(o=>`<option value="${esc(o.id)}"${o.id===v?' selected':''}${o.enabled===false?' disabled':''}>${esc(o.title)}</option>`).join('')}</select><span>${esc(c.label)}${req}</span>${ic('chev',24,'chev')}</label>${help}`;
    case 'RadioButtonsGroup':return `<fieldset class=rg data-group="${esc(c.name)}"${rq}><legend>${esc(c.label)}${req}</legend>${c.description?`<div class=d>${esc(c.description)}</div>`:''}${opts.map(o=>row(o,'radio',o.id===v)).join('')}</fieldset>`;
    case 'CheckboxGroup':return `<fieldset class=rg data-group="${esc(c.name)}"${rq}><legend>${esc(c.label)}${req}</legend>${c.description?`<div class=d>${esc(c.description)}</div>`:''}${opts.map(o=>row(o,'checkbox',[].concat(v||[]).includes(o.id))).join('')}</fieldset>`;
    case 'OptIn':return `<label class=optin><input class=chk type=checkbox data-optin ${nm}${v===true?' checked':''}${rq}><span>${fmt(c.label)}${req}</span></label>`;
    case 'Image':return c.src?`<img src="data:image/png;base64,${esc(c.src)}" alt="${esc(c['alt-text']||'')}">`:'';
    case 'EmbeddedLink':return `<a class=elink>${esc(c.text)}</a>`;
    default:return ''}}
async function openFlow(p,a){
  const r=await fetch('/phone/api/flow/'+encodeURIComponent(a.flow));if(!r.ok)return toast(p,'Flow '+a.flow+' is not known to this fake');
  p.flow={fj:await r.json(),a,values:{},stack:[]};showScreen(p,a.screen||p.flow.fj.screens[0].id,a.data||{})}
function showScreen(p,id,data){const f=p.flow,s=f.fj.screens.find(x=>x.id===id)||f.fj.screens[0],el=p.el.querySelector('.fl');
  f.stack.push({id:s.id,data});let init={},body='',footer=null;
  for(const c of kids(s.layout?.children||[],[])){if(c.type==='__form'){init=(typeof c.init==='string'?expr(c.init,{},data):c.init)||{};continue}
    if(c.type==='Footer'){footer=c;continue}body+=comp(c,data,init)}
  const back=f.stack.length>1;
  el.innerHTML=`<div class=top><button data-k=fx2 title="${back?'Back':'Close'}">${ic(back?'back':'close')}</button><b>${esc(s.title||'')}</b>${ic('more')}</div>
    <form novalidate>${body}</form><div class=ff><button type=button data-k=fgo>${esc(footer?.label||'Continue')}</button><small>Managed by the business. <a>Learn more</a></small></div>`;
  el.classList.add('open');const form=el.querySelector('form'),go=el.querySelector('[data-k=fgo]');
  const check=()=>{go.disabled=![...form.querySelectorAll('[required]')].every(x=>x.tagName==='FIELDSET'?!!x.querySelector('input:checked'):x.type==='checkbox'?x.checked:x.value.trim()!=='')};
  form.oninput=form.onchange=check;check();
  el.onclick=async e=>{const k=e.target.closest('[data-k]')?.dataset.k;
    if(k==='fx2'){f.stack.pop();if(back){const prev=f.stack.pop();showScreen(p,prev.id,prev.data)}else el.classList.remove('open')}
    if(k!=='fgo'||go.disabled)return;
    for(const x of form.elements){if(!x.name)continue;
      if(x.dataset.multi!==undefined){if(!Array.isArray(f.values[x.name]))f.values[x.name]=[];if(x.checked)f.values[x.name].push(x.value)}
      else if(x.dataset.optin!==undefined)f.values[x.name]=x.checked;else if(x.type==='radio'){if(x.checked)f.values[x.name]=x.value}
      else if(x.value!=='')f.values[x.name]=x.value}
    const act=footer?.['on-click-action']||{name:'complete'},i=f.fj.screens.findIndex(x=>x.id===s.id);
    let next=act.name==='navigate'?act.next?.name:act.name==='data_exchange'?f.fj.screens[i+1]?.id:null;
    if(next){const pl={};for(const[k2,v2]of Object.entries(act.payload||{}))pl[k2]=expr(v2,f.values,data);return showScreen(p,next,pl)}
    go.disabled=true;
    const res=await fetch('/phone/api/flow_submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wa_id:p.wa,name:p.name,flow_id:f.a.flow,flow_token:f.a.token,data:f.a.data||{},values:f.values,context_id:f.a.ctx})});
    if(!res.ok){go.disabled=false;return toast(p,'Submit failed: '+res.status)}
    el.classList.remove('open');p.stick=true;poll()}}

/* ---------- render + poll ---------- */
function setText(el,v){if(el.textContent!==v)el.textContent=v}
function render(p){const r=p.data,w=p.el;if(!r)return;
  setText(w.querySelector('.clk'),new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}));
  setText(w.querySelector('.nm'),r.name);setText(w.querySelector('.av'),initials(r.name));
  const s=w.querySelector('.s');setText(s,r.typing?'typing…':'Business account');s.classList.toggle('ty',!!r.typing);
  const chip=w.querySelector('.chip'),left=r.last_in?r.last_in+86400-r.now:-1;
  if(left>0){setText(chip,`24h window · ${Math.floor(left/3600)}h ${Math.floor(left%3600/60)}m left`);chip.className='chip on'}
  else{setText(chip,'24h window closed · templates only');chip.className='chip off'}
  chip.title='Customer service window: free-form messages are allowed for 24h after the person last wrote';
  const th=w.querySelector('.th'),atBottom=th.scrollHeight-th.scrollTop-th.clientHeight<60;
  reconcile(w.querySelector('.list'),items(p,r),p.cache);
  if(p.first||atBottom||p.stick){th.scrollTop=th.scrollHeight;if(p.data.messages.length)p.first=false;p.stick=false}}
async function load(p){const r=await fetch('/phone/api/thread?wa_id='+encodeURIComponent(p.wa));if(!r.ok)return;p.data=await r.json();
  for(const m of p.data.messages)if(m.kind==='audio'&&!(m.id in DUR)){DUR[m.id]=0;
    fetch(mediaSrc(m.body.audio)).then(x=>x.ok?x.arrayBuffer():Promise.reject()).then(b=>new OfflineAudioContext(1,1,44100).decodeAudioData(b)).then(buf=>{DUR[m.id]=buf.duration;render(p)}).catch(()=>{})}
  render(p)}
let busy=false;
async function poll(){if(busy)return;busy=true;try{await Promise.all(phones.map(load))}finally{busy=false}}
async function meta(){try{CONTACTS=(await (await fetch('/phone/api/contacts')).json()).contacts;FIXTURES=(await (await fetch('/phone/api/fixtures')).json()).files}catch(e){}
  const a=document.getElementById('add'),want='<option value="">+ open a phone…</option>'+CONTACTS.map(w=>`<option value="${esc(w)}">+${esc(w)}${KNOWN[w]?' · '+esc(KNOWN[w]):''}</option>`).join('');
  if(a._h!==want){a.innerHTML=want;a._h=want}phones.forEach(opts)}
function addPhone(wa){const p={wa,name:KNOWN[wa]||wa};phones.push(p);document.getElementById('phones').append(shell(p));return p}
function save(){const q=phones.map(p=>p.wa).join(',');store('fw.phones',q);history.replaceState(null,'','?wa='+q)}
document.getElementById('add').onchange=e=>{if(e.target.value){addPhone(e.target.value);save();poll()}e.target.value=''};
function theme(t){document.documentElement.dataset.theme=t;document.getElementById('theme').textContent=t==='dark'?'Light theme':'Dark theme';store('fw.theme',t)}
document.getElementById('theme').onclick=()=>theme(document.documentElement.dataset.theme==='dark'?'light':'dark');
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelectorAll('.scrim.open,.fl.open,.menu.open').forEach(x=>x.classList.remove('open'))});
const q=new URLSearchParams(location.search);theme(q.get('theme')||store('fw.theme')||'dark');
(q.get('wa')||store('fw.phones')||'40712000001,40712000002').split(',').filter(Boolean).forEach(addPhone);
meta().then(poll);setInterval(poll,1500);setInterval(meta,6000);
</script>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-secret", required=True, help="the 32-hex App Secret you enter in Studio")
    parser.add_argument("--port", type=int, default=58123)
    parser.add_argument("--state", default="", help="JSON file that keeps templates, callback and chats across restarts")
    parser.add_argument("--fixtures", default="", help="directory of images, documents and voice notes the phone can attach")
    args = parser.parse_args()
    global STATE_FILE, FIXTURES
    STATE_FILE, FIXTURES = args.state, args.fixtures
    if STATE_FILE and os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state.update(json.load(f))
        global _ids
        _ids = itertools.count(10_000 + len(state["messages"]) * 10)
    state["app_secret"] = args.app_secret
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
