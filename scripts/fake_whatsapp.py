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
back as an ``nfm_reply``, like Meta's.  State lives in memory.

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
    _log("in", wa_id, "interactive", message)
    await _post_webhook({"contacts": [{"profile": {"name": name}, "wa_id": wa_id}], "messages": [message]})
    return {"ok": True, "response_json": response}


MEDIA: dict[str, tuple[bytes, str]] = {}


@app.get("/media-bytes/{media_id}")
async def media_bytes(media_id: str):
    from fastapi.responses import Response
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
        if body.get("typing_indicator"):
            state.setdefault("typing", []).append({"ts": time.time(), "message_id": body.get("message_id")})
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
    elif b.get("media_path"):
        import mimetypes, pathlib
        path = pathlib.Path(b["media_path"])
        mime = b.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        kind = b.get("kind") or ("image" if mime.startswith("image/") else "audio" if mime.startswith("audio/") else "document")
        media_id = f"media{next(_ids)}"
        MEDIA[media_id] = (path.read_bytes(), mime)
        block = {"id": media_id, "mime_type": mime}
        if b.get("caption"):
            block["caption"] = b["caption"]
        if kind == "document":
            block["filename"] = path.name
        if kind == "audio":
            block["voice"] = True
        message |= {"type": kind, kind: block}
    elif b.get("template_button"):
        message |= {"type": "button", "button": {"text": b["template_button"], "payload": b["template_button"]}}
    else:
        message |= {"type": "text", "text": {"body": b["text"]}}
    _log("in", wa_id, message["type"], message)
    await _post_webhook({"contacts": [{"profile": {"name": name}, "wa_id": wa_id}], "messages": [message]})
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
    msgs = []
    for text in b["texts"]:
        m = {"from": b["wa_id"], "id": _wamid(), "timestamp": str(int(time.time())), "type": "text", "text": {"body": text}}
        _log("in", b["wa_id"], "text", m)
        msgs.append(m)
    await _post_webhook({"contacts": [{"profile": {"name": b.get("name", b["wa_id"])}, "wa_id": b["wa_id"]}], "messages": msgs})
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
    return {"messages": out, "window_open": _window_open(wa_id), "name": state["verified_name"]}


@app.get("/phone/api/state")
async def debug_state():
    return {k: v for k, v in state.items() if k not in ("messages", "app_secret", "templates")}


@app.get("/phone", response_class=HTMLResponse)
async def phone():
    return PHONE_HTML


PHONE_HTML = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Fake WhatsApp</title>
<style>
body{margin:0;font:15px system-ui;background:#0b141a;color:#e9edef;display:flex;gap:16px;padding:16px;flex-wrap:wrap}
.ph{width:380px;height:88vh;background:#111b21;border-radius:18px;display:flex;flex-direction:column;overflow:hidden}
.hd{background:#202c33;padding:10px 14px}.hd b{display:block}.hd small{color:#8696a0}
.th{flex:1;overflow:auto;padding:10px;background:#0b141a}
.m{max-width:85%;margin:4px 0;padding:6px 9px;border-radius:8px;white-space:pre-wrap;word-wrap:break-word}
.in{margin-left:auto;background:#005c4b}.out{background:#202c33}
.hdr{font-weight:700;margin-bottom:4px}.ftr{color:#8696a0;font-size:12px;margin-top:4px}
.btn{display:block;width:100%;margin-top:4px;background:#2a3942;color:#53bdeb;border:0;border-radius:6px;padding:7px;cursor:pointer;font:inherit}
.meta{color:#8696a0;font-size:11px;text-align:right}
form{display:flex;gap:6px;padding:8px;background:#202c33}input{flex:1;border:0;border-radius:8px;padding:9px;background:#2a3942;color:#e9edef}
#sheet{position:fixed;inset:0;background:#000a;display:none;align-items:flex-end;justify-content:center}
#sheet .fl{width:380px;max-height:85vh;overflow:auto;background:#111b21;border-radius:14px 14px 0 0;padding:14px}
#sheet form{display:block;background:none;padding:0}#sheet label{display:block;margin:10px 0 4px;color:#8696a0}
#sheet input,#sheet select,#sheet textarea{width:100%;box-sizing:border-box;border:0;border-radius:8px;padding:9px;background:#2a3942;color:#e9edef;font:inherit}
#sheet input[type=radio],#sheet input[type=checkbox]{width:auto;margin-right:6px}#sheet .opt{display:block;color:#e9edef;margin:4px 0}
#sheet h3{margin:12px 0 6px}#sheet p{margin:6px 0;color:#d1d7db}
</style>
<div id=root></div><div id=sheet></div>
<script>
const phones=[{wa:'40712000001',name:'Ion Pacient',label:'Pacient'},{wa:'40712000002',name:'Dr. Radu Pretorian',label:'Medic'}];
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function send(p,extra){await fetch('/phone/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wa_id:p.wa,name:p.name,...extra})});draw()}
function bubble(p,m){
  const b=m.body;let h='';
  if(m.dir==='in'){const nfm=b.interactive?.nfm_reply;const t=nfm?'📋 '+Object.entries(JSON.parse(nfm.response_json)).filter(([k])=>k!=='flow_token').map(([k,v])=>k+': '+v).join('\n'):b.text?.body??b.interactive?.button_reply?.title??b.interactive?.list_reply?.title??b.button?.text;return `<div class="m in">${esc(t)}</div>`}
  if(m.kind==='text')h=esc(b.text?.body);
  else if(m.kind==='template'){const c=m.template?.components||[];const g=t=>c.find(x=>x.type===t);
    h=(g('HEADER')?`<div class=hdr>${esc(g('HEADER').text)}</div>`:'')+esc(g('BODY')?.text||'[template '+b.template?.name+']')+(g('FOOTER')?`<div class=ftr>${esc(g('FOOTER').text)}</div>`:'');
    for(const x of g('BUTTONS')?.buttons||[]){
      if(x.type==='FLOW'){const tok=(b.template?.components||[]).find(c=>c.sub_type==='flow')?.parameters?.[0]?.action?.flow_token||'unused';
        h+=flowBtn(x.text,x.flow_id,tok,x.navigate_screen,{},m.id)}
      else h+=`<button class=btn data-k=tb data-v="${esc(x.text)}">${esc(x.text)}</button>`}}
  else if(m.kind==='interactive'){const i=b.interactive;h=(i.header?.text?`<div class=hdr>${esc(i.header.text)}</div>`:'')+esc(i.body?.text)+(i.footer?`<div class=ftr>${esc(i.footer.text)}</div>`:'');
    if(i.type==='button')for(const x of i.action.buttons)h+=`<button class=btn data-k=br data-id="${esc(x.reply.id)}" data-v="${esc(x.reply.title)}">${esc(x.reply.title)}</button>`;
    if(i.type==='list')for(const s of i.action.sections)for(const r of s.rows)h+=`<button class=btn data-k=lr data-id="${esc(r.id)}" data-v="${esc(r.title)}">${esc(r.title)}${r.description?' · '+esc(r.description):''}</button>`;
    if(i.type==='flow'){const f=i.action.parameters;h+=flowBtn(f.flow_cta,f.flow_id,f.flow_token,f.flow_action_payload?.screen,f.flow_action_payload?.data||{},m.id)}}
  else h=esc('['+m.kind+'] '+JSON.stringify(b).slice(0,200));
  return `<div class="m out">${h}<div class=meta>${m.status}</div></div>`}
const flowBtn=(text,flow,token,screen,data,ctx)=>`<button class=btn data-k=flow data-flow="${esc(flow)}" data-token="${esc(token)}" data-screen="${esc(screen||'')}" data-data="${esc(JSON.stringify(data))}" data-ctx="${esc(ctx)}">📋 ${esc(text)}</button>`;
const val=(x,data)=>{const r=typeof x==='string'&&/^\$\{data\.(\w+)\}$/.exec(x);return r?data[r[1]]:x};
function walk(n,out){if(Array.isArray(n))n.forEach(c=>walk(c,out));else if(n&&typeof n==='object'){out.push(n);walk(n.children||[],out)}return out}
function field(c,data,init){
  const v=val(c['init-value']??init[c.name],data)??'',lb=`<label>${esc(c.label)}${c.required?' *':''}</label>`,nm=`name="${esc(c.name)}"`,rq=c.required?' required':'';
  const opts=val(c['data-source'],data)||[];
  switch(c.type){
    case 'TextHeading':case 'TextSubheading':return `<h3>${esc(c.text)}</h3>`;
    case 'TextBody':case 'TextCaption':return `<p>${esc(c.text)}</p>`;
    case 'TextInput':{const t={number:'number',email:'email',phone:'tel',password:'password'}[c['input-type']]||'text';return lb+`<input type=${t} ${nm} value="${esc(v)}"${rq}>`}
    case 'TextArea':return lb+`<textarea ${nm} rows=3${rq}>${esc(v)}</textarea>`;
    case 'DatePicker':return lb+`<input type=date ${nm} value="${esc(v)}"${rq}>`;
    case 'Dropdown':return lb+`<select ${nm}${rq}><option value=""></option>${opts.map(o=>`<option value="${esc(o.id)}"${o.id===v?' selected':''}>${esc(o.title)}</option>`).join('')}</select>`;
    case 'RadioButtonsGroup':return lb+opts.map(o=>`<span class=opt><input type=radio ${nm} value="${esc(o.id)}"${o.id===v?' checked':''}${rq}>${esc(o.title)}</span>`).join('');
    case 'CheckboxGroup':return lb+opts.map(o=>`<span class=opt><input type=checkbox data-multi ${nm} value="${esc(o.id)}"${(v||[]).includes?.(o.id)?' checked':''}>${esc(o.title)}</span>`).join('');
    case 'OptIn':return `<span class=opt><input type=checkbox data-optin ${nm}${v===true?' checked':''}${rq}>${esc(c.label)}</span>`;
    case 'Footer':return `<button class=btn type=submit>${esc(c.label)}</button>`;
    default:return ''}}
async function openFlow(p,d){
  const fj=await (await fetch('/phone/api/flow/'+d.flow)).json(),data=JSON.parse(d.data||'{}');
  const screens=[...fj.screens].sort((a,b)=>(b.id===d.screen)-(a.id===d.screen));
  let html='',footer='';
  for(const s of screens){html+=`<h3>${esc(s.title||s.id)}</h3>`;for(const c of walk(s.layout?.children||[],[])){
    const init=c.type==='Form'?c['init-values']||{}:{};if(c.type==='Form'){walk(c.children,[]).forEach(x=>x.__init=init);continue}
    const f=field(c,data,c.__init||{});if(c.type==='Footer')footer=f;else html+=f}}
  sheet.innerHTML=`<div class=fl><form>${html}${footer||'<button class=btn type=submit>Trimite</button>'}<button class=btn type=button id=cl>Închide</button></form></div>`;
  sheet.style.display='flex';sheet.querySelector('#cl').onclick=()=>sheet.style.display='none';
  sheet.querySelector('form').onsubmit=async e=>{e.preventDefault();const values={};
    for(const el of e.target.elements){if(!el.name)continue;
      if(el.dataset.multi!==undefined){(values[el.name]??=[]);if(el.checked)values[el.name].push(el.value)}
      else if(el.dataset.optin!==undefined)values[el.name]=el.checked;
      else if(el.type==='radio'){if(el.checked)values[el.name]=el.value}
      else if(el.value!=='')values[el.name]=el.value}
    await fetch('/phone/api/flow_submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wa_id:p.wa,name:p.name,flow_id:d.flow,flow_token:d.token,data,values,context_id:d.ctx})});
    sheet.style.display='none';draw()}}
async function draw(){
  let out='';
  for(const p of phones){const r=await (await fetch('/phone/api/thread?wa_id='+p.wa)).json();
    out+=`<div class=ph data-wa=${p.wa}><div class=hd><b>${esc(r.name)}</b><small>${p.label}: ${p.name} (+${p.wa}) · fereastra 24h ${r.window_open?'deschisă':'închisă'}</small></div><div class=th>${r.messages.map(m=>bubble(p,m)).join('')}</div><form><input placeholder="Mesaj"></form></div>`}
  const scroll=[...document.querySelectorAll('.th')].map(t=>t.scrollHeight-t.scrollTop-t.clientHeight<40);
  const focus=document.activeElement?.closest?.('.ph')?.dataset.wa;const val=document.activeElement?.value;
  root.innerHTML=out;
  document.querySelectorAll('.th').forEach((t,i)=>{if(scroll[i]!==false)t.scrollTop=t.scrollHeight});
  document.querySelectorAll('.ph').forEach(el=>{const p=phones.find(x=>x.wa===el.dataset.wa);
    el.querySelector('form').onsubmit=e=>{e.preventDefault();const v=e.target[0].value.trim();if(v)send(p,{text:v})};
    if(focus===p.wa){const i=el.querySelector('input');i.value=val||'';i.focus()}
    el.querySelectorAll('.btn').forEach(b=>b.onclick=()=>{const k=b.dataset.k;
      if(k==='tb')send(p,{template_button:b.dataset.v});
      if(k==='br')send(p,{button_reply:{id:b.dataset.id,title:b.dataset.v}});
      if(k==='lr')send(p,{list_reply:{id:b.dataset.id,title:b.dataset.v}});
      if(k==='flow')openFlow(p,b.dataset)})})}
draw();setInterval(()=>{if(!document.activeElement||document.activeElement.tagName!=='INPUT'||!document.activeElement.value)draw()},1500);
</script>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-secret", required=True, help="the 32-hex App Secret you enter in Studio")
    parser.add_argument("--port", type=int, default=58123)
    parser.add_argument("--state", default="", help="JSON file that keeps templates, callback and chats across restarts")
    args = parser.parse_args()
    global STATE_FILE
    STATE_FILE = args.state
    if STATE_FILE and os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state.update(json.load(f))
        global _ids
        _ids = itertools.count(10_000 + len(state["messages"]) * 10)
    state["app_secret"] = args.app_secret
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
