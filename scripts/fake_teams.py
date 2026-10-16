"""A fake Microsoft Teams (Bot Framework + Entra ID) plus a browser chat, for testing locally.

Point the runtime at it:

    SUROGATES_TEAMS_LOGIN_BASE=http://127.0.0.1:58125
    SUROGATES_TEAMS_OPENID_URL=http://127.0.0.1:58125/v1/.well-known/openidconfiguration
    SUROGATES_CHANNELS_TEAMS_ENABLED=true

then connect Teams in Studio with the dev credentials

    App ID     00000000-0000-4000-8000-00000000fea5
    Tenant ID  00000000-0000-4000-8000-0000000000aa
    Secret     fake-teams-secret-0123456789

and open http://127.0.0.1:58125/teams to write as a person, in a personal
chat or in a team channel (with or without @mentioning the bot).

It speaks the real wire format and signs like the real thing — there is no
"auth off" switch.  It serves OpenID metadata and a JWKS for an RSA key made
at first start (kept in ``--state`` so a restart does not invalidate the
runtime's cached key), mints RS256 tokens (``iss`` api.botframework.com,
``aud`` the App ID, a ``serviceurl`` claim naming this server) for every
activity it posts to ``--messaging-endpoint``, checks client credentials on
``/{tenant}/oauth2/v2.0/token``, and accepts only the tokens it issued on the
connector API (``/v3/conversations/{id}/activities``: POST, PUT, typing).

Scripts drive it with ``POST /api/teams/send``::

    {"user": "Ana Pop", "text": "hello", "conversation": "personal" | "channel",
     "mention": true, "thread": "<root activity id, channel only>"}

and ``POST /api/teams/install`` (``{"user": ..., "conversation": ...}``) sends
the ``conversationUpdate`` an app install produces.  Every message in and out
is logged to ``--state``.

    .venv/bin/python scripts/fake_teams.py --state /tmp/fake-teams.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
import urllib.parse
import uuid

import httpx
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jose import jwk, jwt

APP_ID = "00000000-0000-4000-8000-00000000fea5"
TENANT_ID = "00000000-0000-4000-8000-0000000000aa"
APP_SECRET = "fake-teams-secret-0123456789"
BOT_NAME = "Agent"
KID = "fake-teams-key-1"
ISSUER = "https://api.botframework.com"
SCOPE = "https://api.botframework.com/.default"
CHANNEL = "19:fake-general@thread.tacv2"
THREAD_MARK = ";messageid="

app = FastAPI()
_ids = itertools.count(int(time.time() * 1000))
state: dict = {"pem": "", "messages": [], "conversations": {}, "typing": {}, "tokens": []}
config = {
    "port": 58125,
    "state_file": "",
    "endpoint": f"http://127.0.0.1:8004/teams/{APP_ID}",
}


def _persist() -> None:
    if config["state_file"]:
        with open(config["state_file"], "w") as f:
            json.dump(state, f)


def _pem() -> str:
    if not state["pem"]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        state["pem"] = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        _persist()
    return state["pem"]


def service_url() -> str:
    return f"http://127.0.0.1:{config['port']}/"


def mint_token(service: str, *, app_id: str = APP_ID, ttl: int = 3600) -> str:
    """A Bot Framework token as the connector service sends with each activity."""
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": app_id, "nbf": now - 5, "exp": now + ttl,
        "serviceurl": service,
    }
    return jwt.encode(claims, _pem(), algorithm="RS256", headers={"kid": KID})


def _person(name: str) -> dict:
    aad = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fake-teams:{name}"))
    return {"id": f"29:{aad[:8]}", "name": name, "aadObjectId": aad}


def _bot() -> dict:
    return {"id": f"28:{APP_ID}", "name": BOT_NAME}


def _conversation(kind: str, person: dict, thread: str | None, activity_id: str) -> dict:
    if kind == "personal":
        conv = {"id": f"a:fake-personal-{person['aadObjectId']}", "conversationType": "personal"}
    else:
        conv = {
            "id": f"{CHANNEL}{THREAD_MARK}{thread or activity_id}",
            "conversationType": "channel", "name": "General",
        }
    conv["tenantId"] = TENANT_ID
    return conv


def build_activity(
    user: str, text: str, *, conversation: str = "personal", mention: bool = False,
    thread: str | None = None, service: str | None = None, kind: str = "message",
) -> dict:
    activity_id = str(next(_ids))
    person = _person(user)
    activity: dict = {
        "type": kind,
        "id": activity_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "channelId": "msteams",
        "serviceUrl": service or service_url(),
        "from": person,
        "recipient": _bot(),
        "conversation": _conversation(conversation, person, thread, activity_id),
        "channelData": {"tenant": {"id": TENANT_ID}},
    }
    if kind == "conversationUpdate":
        activity["membersAdded"] = [_bot(), person]
        return activity
    activity["textFormat"] = "plain"
    if mention:
        tag = f"<at>{BOT_NAME}</at>"
        text = f"{tag} {text}"
        activity["entities"] = [{"type": "mention", "text": tag, "mentioned": _bot()}]
    activity["text"] = text
    return activity


def _split(conversation_id: str) -> tuple[str, str | None]:
    base, sep, root = conversation_id.partition(THREAD_MARK)
    return base, (root or None) if sep else None


def _log(direction: str, conversation_id: str, *, user: str, text: str, msg_id: str,
         thread: str | None = None, attachments: list | None = None) -> dict:
    entry = {
        "id": msg_id, "dir": direction, "conversation": conversation_id, "thread": thread,
        "user": user, "text": text, "attachments": attachments or [], "ts": time.time(),
    }
    state["messages"].append(entry)
    _persist()
    return entry


async def _post_activity(activity: dict) -> int:
    base, root = _split(activity["conversation"]["id"])
    state["conversations"][base] = activity["conversation"]["conversationType"]
    if activity["type"] == "message":
        _log("in", base, user=activity["from"]["name"], text=activity.get("text", ""),
             msg_id=activity["id"], thread=root)
    token = mint_token(activity["serviceUrl"])
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                config["endpoint"], json=activity, headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError:
        return 0  # the runtime is not listening
    return resp.status_code


# ---------------------------------------------------------------------------
# OpenID metadata and keys (login.botframework.com)
# ---------------------------------------------------------------------------


@app.get("/v1/.well-known/openidconfiguration")
async def openid(request: Request):
    return {
        "issuer": ISSUER,
        "authorization_endpoint": "https://invalid.botframework.com",
        "jwks_uri": f"{request.base_url}v1/.well-known/keys",
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["private_key_jwt"],
    }


@app.get("/v1/.well-known/keys")
async def keys():
    public = jwk.construct(_pem(), "RS256").public_key().to_dict()
    return {"keys": [{**public, "kid": KID, "use": "sig", "endorsements": ["msteams"]}]}


# ---------------------------------------------------------------------------
# Entra ID token endpoint (login.microsoftonline.com)
# ---------------------------------------------------------------------------


def _entra_error(status: int, error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


@app.post("/{tenant}/oauth2/v2.0/token")
async def token(tenant: str, request: Request):
    form = dict(await request.form())
    if tenant != TENANT_ID:
        return _entra_error(400, "invalid_tenant", f"AADSTS90002: Tenant '{tenant}' not found.")
    if form.get("grant_type") != "client_credentials" or form.get("scope") != SCOPE:
        return _entra_error(400, "invalid_request", "AADSTS900144: grant_type or scope is wrong.")
    if form.get("client_id") != APP_ID:
        return _entra_error(400, "unauthorized_client", "AADSTS700016: Application not found in the directory.")
    if form.get("client_secret") != APP_SECRET:
        return _entra_error(401, "invalid_client", "AADSTS7000215: Invalid client secret provided.")
    issued = f"fake-bf-{uuid.uuid4().hex}"
    # Kept in --state: the runtime caches a token for an hour, and a restart
    # of this fake must not turn every reply into a 401.
    state["tokens"] = [*state["tokens"][-99:], issued]
    _persist()
    return {"token_type": "Bearer", "expires_in": 3599, "ext_expires_in": 3599, "access_token": issued}


# ---------------------------------------------------------------------------
# Connector API (smba.trafficmanager.net)
# ---------------------------------------------------------------------------


def _connector_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _authorised(request: Request) -> bool:
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    return scheme.lower() == "bearer" and value in state["tokens"]


@app.post("/v3/conversations/{conversation_id:path}/activities")
async def post_activity(conversation_id: str, request: Request):
    if not _authorised(request):
        return _connector_error(401, "Unauthorized", "Authorization has been denied for this request.")
    base, root = _split(urllib.parse.unquote(conversation_id))
    if base not in state["conversations"]:
        return _connector_error(404, "ConversationNotFound", "Conversation not found.")
    body = await request.json()
    if body.get("type") == "typing":
        state["typing"][base] = time.time()
        return JSONResponse({}, status_code=201)
    text = body.get("text") or ""
    if len(json.dumps(body)) > 28_000:
        return _connector_error(413, "MessageSizeTooBig", "Activity body too large.")
    entry = _log("out", base, user=BOT_NAME, text=text, msg_id=str(next(_ids)),
                 thread=root or body.get("replyToId"), attachments=body.get("attachments"))
    state["typing"].pop(base, None)
    return JSONResponse({"id": entry["id"]}, status_code=201)


@app.put("/v3/conversations/{conversation_id:path}/activities/{activity_id}")
async def update_activity(conversation_id: str, activity_id: str, request: Request):
    if not _authorised(request):
        return _connector_error(401, "Unauthorized", "Authorization has been denied for this request.")
    body = await request.json()
    for entry in state["messages"]:
        if entry["id"] == activity_id and entry["dir"] == "out":
            entry["text"], entry["edited"] = body.get("text") or "", True
            _persist()
            return {"id": activity_id}
    return _connector_error(404, "ActivityNotFound", "Activity not found.")


# ---------------------------------------------------------------------------
# Script and browser API
# ---------------------------------------------------------------------------


@app.post("/api/teams/send")
async def api_send(request: Request):
    """Write as a person: a personal chat, or a channel post (optionally in a thread)."""
    b = await request.json()
    activity = build_activity(
        b.get("user") or "Ana Pop", b.get("text") or "",
        conversation=b.get("conversation") or "personal",
        mention=bool(b.get("mention")), thread=b.get("thread"),
    )
    status = await _post_activity(activity)
    return {"ok": 200 <= status < 300, "status": status, "activity_id": activity["id"],
            "conversation_id": activity["conversation"]["id"]}


@app.post("/api/teams/install")
async def api_install(request: Request):
    """The person installs the app: Teams sends the bot a conversationUpdate."""
    b = await request.json()
    activity = build_activity(
        b.get("user") or "Ana Pop", "", conversation=b.get("conversation") or "personal",
        kind="conversationUpdate",
    )
    status = await _post_activity(activity)
    return {"ok": 200 <= status < 300, "status": status}


@app.get("/api/teams/messages")
async def api_messages(user: str = "Ana Pop"):
    personal = f"a:fake-personal-{_person(user)['aadObjectId']}"
    def _typing(conv: str) -> bool:
        return time.time() - state["typing"].get(conv, 0) < 4
    return {
        "personal": [m for m in state["messages"] if m["conversation"] == personal],
        "channel": [m for m in state["messages"] if m["conversation"] == CHANNEL],
        "typing": {"personal": _typing(personal), "channel": _typing(CHANNEL)},
    }


@app.get("/teams", response_class=HTMLResponse)
async def ui():
    return UI_HTML


UI_HTML = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Fake Teams</title>
<style>
:root{--bg:#f5f5f5;--pane:#fff;--tx:#242424;--mut:#616161;--line:#e0e0e0;--acc:#5b5fc7;--mine:#e8ebfa;--bot:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#1f1f1f;--pane:#292929;--tx:#fff;--mut:#adadad;--line:#3d3d3d;--acc:#7f85f5;--mine:#3d3e66;--bot:#333}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/20px "Segoe UI",-apple-system,Roboto,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;background:var(--acc);color:#fff}header b{margin-right:auto}
header input{border:0;border-radius:4px;padding:5px 8px;font:inherit}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;padding:16px}
section{background:var(--pane);border:1px solid var(--line);border-radius:8px;display:flex;flex-direction:column;height:calc(100vh - 100px);min-height:400px}
h2{margin:0;padding:10px 14px;font-size:15px;border-bottom:1px solid var(--line)}h2 small{color:var(--mut);font-weight:400}
.th{flex:1;overflow-y:auto;padding:10px 14px;display:flex;flex-direction:column;gap:8px}
.m{max-width:85%;padding:6px 10px;border-radius:6px;background:var(--bot);border:1px solid var(--line);white-space:pre-wrap;overflow-wrap:anywhere}
.m.in{align-self:flex-end;background:var(--mine);border-color:transparent}.m small{display:block;color:var(--mut);font-size:11.5px}
.m img{max-width:100%;border-radius:4px;margin-top:4px}.m.reply{margin-left:22px}
.ty{color:var(--mut);font-style:italic;padding:0 14px 6px;min-height:20px}
form{display:flex;gap:8px;padding:10px;border-top:1px solid var(--line);align-items:center;flex-wrap:wrap}
form input[type=text]{flex:1;min-width:120px;padding:8px;border:1px solid var(--line);border-radius:4px;background:var(--pane);color:var(--tx);font:inherit}
form button{background:var(--acc);color:#fff;border:0;border-radius:4px;padding:8px 14px;font:inherit;cursor:pointer}
form label{color:var(--mut);font-size:13px;display:flex;gap:4px;align-items:center}
</style>
<header><b>Fake Teams</b><label>You are <input id=who value="Ana Pop"></label><button id=install>Install app</button></header>
<main>
<section><h2>Chat with Agent <small>personal</small></h2><div class=th id=personal></div><div class=ty id=ty-personal></div>
<form id=f-personal><input type=text placeholder="Type a message" autocomplete=off><button>Send</button></form></section>
<section><h2>General <small>team channel · the bot hears only @mentions</small></h2><div class=th id=channel></div><div class=ty id=ty-channel></div>
<form id=f-channel><input type=text placeholder="Start a post or reply" autocomplete=off>
<label><input type=checkbox id=mention checked>@Agent</label><label>thread <select id=thread><option value="">new post</option></select></label><button>Send</button></form></section>
</main>
<script>
const $=id=>document.getElementById(id),esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function body(m){const att=(m.attachments||[]).map(a=>a.contentUrl&&a.contentType.startsWith('image/')?`<img src="${esc(a.contentUrl)}" alt="${esc(a.name||'')}">`:`<div>[${esc(a.name||a.contentType)}]</div>`).join('');
  return `<small>${esc(m.user)}${m.edited?' · edited':''}</small>${esc(m.text)}${att}`}
function render(el,list){const near=el.scrollHeight-el.scrollTop-el.clientHeight<60,html=list.map(m=>`<div class="m ${m.dir}${m.thread&&m.thread!==m.id?' reply':''}">${body(m)}</div>`).join('');
  if(el._h!==html){el.innerHTML=html;el._h=html;if(near)el.scrollTop=el.scrollHeight}}
async function poll(){const r=await fetch('/api/teams/messages?user='+encodeURIComponent($('who').value));if(!r.ok)return;const d=await r.json();
  render($('personal'),d.personal);render($('channel'),d.channel);
  $('ty-personal').textContent=d.typing.personal?'Agent is typing…':'';$('ty-channel').textContent=d.typing.channel?'Agent is typing…':'';
  const sel=$('thread'),roots=d.channel.filter(m=>m.dir==='in'&&(!m.thread||m.thread===m.id)),cur=sel.value,
    opts='<option value="">new post</option>'+roots.map(m=>`<option value="${esc(m.id)}">${esc(m.text.slice(0,30))}</option>`).join('');
  if(sel._h!==opts){sel.innerHTML=opts;sel._h=opts;sel.value=cur}}
async function send(conversation,input,extra){const text=input.value.trim();if(!text)return;input.value='';
  await fetch('/api/teams/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:$('who').value,text,conversation,...extra})});poll()}
$('f-personal').onsubmit=e=>{e.preventDefault();send('personal',e.target.querySelector('input'),{})};
$('f-channel').onsubmit=e=>{e.preventDefault();send('channel',e.target.querySelector('input[type=text]'),{mention:$('mention').checked,thread:$('thread').value||null})};
$('install').onclick=()=>fetch('/api/teams/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:$('who').value})});
poll();setInterval(poll,1500);
</script>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=58125)
    parser.add_argument("--state", default="", help="JSON file that keeps the signing key and chats across restarts")
    parser.add_argument(
        "--messaging-endpoint", default="",
        help=f"where activities are posted (default http://127.0.0.1:8004/teams/{APP_ID})",
    )
    args = parser.parse_args()
    config["port"], config["state_file"] = args.port, args.state
    if args.messaging_endpoint:
        config["endpoint"] = args.messaging_endpoint
    if args.state and os.path.exists(args.state):
        with open(args.state) as f:
            state.update(json.load(f))
    _pem()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
