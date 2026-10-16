"""A fake Discord (REST + Gateway) plus a browser client, for testing Discord locally.

Point the runtime at it:

    SUROGATES_DISCORD_API_BASE=http://127.0.0.1:58126/api/v10
    SUROGATES_CHANNELS_DISCORD_ENABLED=true

(and ops' Discord provisioner at the same base), then connect Discord in
Studio with the dev credentials below and open http://127.0.0.1:58126/discord
to write as a person: a DM, or in ``#general`` of the server "Surogate Lab",
with or without an @mention of the bot.

Dev credentials (fake, safe to commit):

    application id  1300000000000000001   (also the bot's user id, as on Discord)
    bot token       fake-discord-bot-token.local.0123456789

Wire format is Discord's own: ``GET /api/v10/gateway/bot`` returns a Gateway
URL on this server; the Gateway speaks HELLO, heartbeat ACK, IDENTIFY → READY,
RESUME → RESUMED (replaying what was missed) and MESSAGE_CREATE.  Like the real
one it rejects a bad token with close code 4004, and without the Message
Content intent it blanks server messages that do not mention the bot.
``--deny-message-content`` makes it refuse that intent with 4014, as Discord
does when the portal toggle is off.  ``POST /api/discord/reconnect`` sends
op 7 RECONNECT to every connection.

Script API: ``POST /api/discord/send`` with
``{"user": "alice"|"bob", "text": "...", "where": "dm"|"general", "mention": true}``.
Messages log to ``--state``.

    .venv/bin/python scripts/fake_discord.py --state /tmp/fake-discord.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

APP_ID = "1300000000000000001"
BOT_TOKEN = "fake-discord-bot-token.local.0123456789"
BOT_USER = {"id": APP_ID, "username": "Surogate", "global_name": None, "discriminator": "0", "bot": True}
GUILD = {"id": "1300000000000000010", "name": "Surogate Lab"}
GENERAL = "1300000000000000011"
USERS = {
    "alice": {"id": "1300000000000000101", "username": "alice", "global_name": "Alice Pop", "discriminator": "0"},
    "bob": {"id": "1300000000000000102", "username": "bob", "global_name": "Bob Ionescu", "discriminator": "0"},
}
DMS = {"1300000000000000201": "alice", "1300000000000000202": "bob"}  # DM channel → user
MESSAGE_CONTENT = 1 << 15

PORT = 58126
HEARTBEAT_MS = 41250
DENY_MESSAGE_CONTENT = False
STATE_FILE = ""

app = FastAPI()
_ids = itertools.count(1400000000000000001)
state: dict = {"messages": []}
typing: dict[str, float] = {}  # channel id → last typing post
#: Gateway sessions: id → {"seq", "intents", "buffer", "ws"}.
sessions: dict[str, dict] = {}


def _persist() -> None:
    if STATE_FILE:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)


def _error(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _authorized(request: Request) -> bool:
    return request.headers.get("authorization") == f"Bot {BOT_TOKEN}"


def _user(key: str) -> dict | None:
    return USERS.get(key) or next((u for u in USERS.values() if u["id"] == key), None)


def _known_channel(channel_id: str) -> bool:
    return channel_id == GENERAL or channel_id in DMS


def _message(channel_id: str, author: dict, content: str, **extra) -> dict:
    msg = {
        "id": str(next(_ids)),
        "type": 19 if extra.get("message_reference") else 0,
        "channel_id": channel_id,
        "author": author,
        "content": content,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "edited_timestamp": None,
        "mentions": [],
        "attachments": [],
        **extra,
    }
    if channel_id == GENERAL:
        msg["guild_id"] = GUILD["id"]
        if not author.get("bot"):
            msg["member"] = {"nick": None, "roles": []}
    state["messages"].append(msg)
    _persist()
    return msg


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


def _for_session(t: str, d: dict, intents: int) -> dict:
    """What this session is allowed to see of the event."""
    if t != "MESSAGE_CREATE" or not d.get("guild_id") or intents & MESSAGE_CONTENT:
        return d
    if d["author"]["id"] == APP_ID or any(m["id"] == APP_ID for m in d.get("mentions") or []):
        return d
    return {**d, "content": "", "attachments": [], "embeds": []}


async def dispatch(t: str, d: dict) -> None:
    for session in sessions.values():
        session["seq"] += 1
        frame = {"op": 0, "t": t, "s": session["seq"], "d": _for_session(t, d, session["intents"])}
        session["buffer"] = (session["buffer"] + [frame])[-1000:]
        if session["ws"] is not None:
            try:
                await session["ws"].send_text(json.dumps(frame))
            except Exception:
                session["ws"] = None


@app.websocket("/")
async def gateway(ws: WebSocket) -> None:
    await ws.accept()
    await ws.send_text(json.dumps({"op": 10, "d": {"heartbeat_interval": HEARTBEAT_MS}}))
    session: dict | None = None
    try:
        while True:
            frame = json.loads(await ws.receive_text())
            op, d = frame.get("op"), frame.get("d")
            if op == 1:
                await ws.send_text(json.dumps({"op": 11}))
            elif op == 2:
                if d.get("token") != BOT_TOKEN:
                    await ws.close(4004, "Authentication failed.")
                    return
                if DENY_MESSAGE_CONTENT and d.get("intents", 0) & MESSAGE_CONTENT:
                    await ws.close(4014, "Disallowed intent(s).")
                    return
                sid = uuid.uuid4().hex
                session = sessions[sid] = {"seq": 1, "intents": d.get("intents", 0), "buffer": [], "ws": ws}
                await ws.send_text(json.dumps({"op": 0, "t": "READY", "s": 1, "d": {
                    "v": 10, "user": BOT_USER, "session_id": sid,
                    "resume_gateway_url": f"ws://127.0.0.1:{PORT}",
                    "guilds": [{"id": GUILD["id"], "unavailable": True}],
                    "application": {"id": APP_ID, "flags": 0},
                }}))
            elif op == 6:
                session = sessions.get(d.get("session_id"))
                if d.get("token") != BOT_TOKEN or session is None:
                    session = None
                    await ws.send_text(json.dumps({"op": 9, "d": False}))
                    continue
                session["ws"] = ws
                for missed in session["buffer"]:
                    if missed["s"] > (d.get("seq") or 0):
                        await ws.send_text(json.dumps(missed))
                session["seq"] += 1
                await ws.send_text(json.dumps({"op": 0, "t": "RESUMED", "s": session["seq"], "d": {}}))
    except WebSocketDisconnect:
        pass
    finally:
        if session is not None and session["ws"] is ws:
            session["ws"] = None


# ---------------------------------------------------------------------------
# REST (the bot's side)
# ---------------------------------------------------------------------------


@app.get("/api/v10/gateway/bot")
async def gateway_bot(request: Request):
    if not _authorized(request):
        return _error(401, 0, "401: Unauthorized")
    return {"url": f"ws://127.0.0.1:{PORT}", "shards": 1, "session_start_limit": {
        "total": 1000, "remaining": 999, "reset_after": 86400000, "max_concurrency": 1,
    }}


@app.get("/api/v10/users/@me")
async def users_me(request: Request):
    if not _authorized(request):
        return _error(401, 0, "401: Unauthorized")
    return BOT_USER


@app.get("/api/v10/applications/@me")
async def application_me(request: Request):
    if not _authorized(request):
        return _error(401, 0, "401: Unauthorized")
    return {"id": APP_ID, "name": "Surogate", "bot_public": True, "bot": BOT_USER, "flags": 0}


@app.post("/api/v10/users/@me/channels")
async def open_dm(request: Request):
    if not _authorized(request):
        return _error(401, 0, "401: Unauthorized")
    user = _user(str((await request.json()).get("recipient_id") or ""))
    if user is None:
        return _error(400, 50033, "Invalid Recipient(s)")
    channel_id = next(c for c, key in DMS.items() if USERS[key] is user)
    return {"id": channel_id, "type": 1, "recipients": [user]}


@app.post("/api/v10/channels/{channel_id}/messages")
async def create_message(channel_id: str, request: Request):
    if not _authorized(request):
        return _error(401, 0, "401: Unauthorized")
    if not _known_channel(channel_id):
        return _error(404, 10003, "Unknown Channel")
    attachments: list[dict] = []
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        body = json.loads(form.get("payload_json") or "{}")
        for key, value in form.multi_items():
            if key.startswith("files[") and hasattr(value, "filename"):
                data = await value.read()
                attachments.append({"id": str(next(_ids)), "filename": value.filename,
                                    "size": len(data), "content_type": value.content_type})
    else:
        body = await request.json()
    content = body.get("content") or ""
    if len(content) > 2000:
        return _error(400, 50035, "Invalid Form Body")
    if not content and not attachments:
        return _error(400, 50006, "Cannot send an empty message")
    extra: dict = {"attachments": attachments}
    if body.get("message_reference"):
        extra["message_reference"] = body["message_reference"]
    typing.pop(channel_id, None)  # a message ends the typing indicator
    msg = _message(channel_id, BOT_USER, content, **extra)
    await dispatch("MESSAGE_CREATE", msg)
    return msg


@app.patch("/api/v10/channels/{channel_id}/messages/{message_id}")
async def edit_message(channel_id: str, message_id: str, request: Request):
    if not _authorized(request):
        return _error(401, 0, "401: Unauthorized")
    msg = next((m for m in state["messages"] if m["id"] == message_id and m["channel_id"] == channel_id), None)
    if msg is None:
        return _error(404, 10008, "Unknown Message")
    content = (await request.json()).get("content") or ""
    if len(content) > 2000:
        return _error(400, 50035, "Invalid Form Body")
    msg["content"] = content
    msg["edited_timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    _persist()
    return msg


@app.delete("/api/v10/channels/{channel_id}/messages/{message_id}")
async def delete_message(channel_id: str, message_id: str, request: Request):
    if not _authorized(request):
        return _error(401, 0, "401: Unauthorized")
    state["messages"] = [m for m in state["messages"] if m["id"] != message_id]
    _persist()
    return Response(status_code=204)


@app.post("/api/v10/channels/{channel_id}/typing")
async def trigger_typing(channel_id: str, request: Request):
    if not _authorized(request):
        return _error(401, 0, "401: Unauthorized")
    if not _known_channel(channel_id):
        return _error(404, 10003, "Unknown Channel")
    typing[channel_id] = time.time()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# The person's side
# ---------------------------------------------------------------------------


@app.post("/api/discord/send")
async def person_send(request: Request):
    body = await request.json()
    user = _user(str(body.get("user") or "alice"))
    if user is None:
        return _error(400, 0, "unknown user; use alice or bob")
    text = str(body.get("text") or "")
    where = body.get("where") or "dm"
    channel_id = GENERAL if where == "general" else next(c for c, k in DMS.items() if USERS[k] is user)
    mentions: list[dict] = []
    if body.get("mention"):
        text = f"<@{APP_ID}> {text}"
        mentions = [BOT_USER]
    msg = _message(channel_id, user, text, mentions=mentions)
    await dispatch("MESSAGE_CREATE", msg)
    return msg


@app.post("/api/discord/reconnect")
async def force_reconnect():
    n = 0
    for session in sessions.values():
        if session["ws"] is not None:
            await session["ws"].send_text(json.dumps({"op": 7, "d": None}))
            n += 1
    return {"reconnect_sent": n}


@app.get("/api/discord/messages")
async def messages(channel: str):
    return {
        "messages": [m for m in state["messages"] if m["channel_id"] == channel],
        "typing": time.time() - typing.get(channel, 0) < 10,
    }


@app.get("/api/discord/state")
async def debug_state():
    return {
        **state,
        "sessions": {sid: {k: v for k, v in s.items() if k not in ("ws", "buffer")} | {"connected": s["ws"] is not None}
                     for sid, s in sessions.items()},
    }


@app.get("/discord", response_class=HTMLResponse)
async def page():
    return _PAGE.replace("__CONFIG__", json.dumps({
        "users": USERS, "dms": DMS, "general": GENERAL, "bot": APP_ID, "guild": GUILD["name"],
    }))


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fake Discord</title>
<style>
  :root { --bg:#313338; --side:#2b2d31; --text:#dbdee1; --muted:#949ba4; --accent:#5865f2; --input:#383a40; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.4 system-ui,sans-serif; display:flex; height:100vh; }
  nav { width:230px; background:var(--side); padding:12px; overflow:auto; }
  nav h2 { font-size:12px; text-transform:uppercase; color:var(--muted); margin:16px 6px 6px; }
  nav button { display:block; width:100%; text-align:left; background:none; border:0; color:var(--muted); padding:6px 8px; border-radius:4px; cursor:pointer; font:inherit; }
  nav button.on, nav button:hover { background:#404249; color:var(--text); }
  main { flex:1; display:flex; flex-direction:column; min-width:0; }
  header { padding:12px 16px; border-bottom:1px solid #1f2023; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  #log { flex:1; overflow:auto; padding:16px; }
  .m { margin:0 0 12px; } .who { font-weight:600; } .bot { color:#a3a6ff; } .t { color:var(--muted); font-size:12px; margin-left:6px; }
  .body { white-space:pre-wrap; word-wrap:break-word; } .ed { color:var(--muted); font-size:11px; }
  .mention { background:rgba(88,101,242,.3); border-radius:3px; padding:0 2px; }
  #typing { height:20px; padding:0 16px; color:var(--muted); font-size:13px; }
  form { display:flex; gap:8px; padding:0 16px 16px; }
  input[type=text] { flex:1; background:var(--input); color:var(--text); border:0; border-radius:8px; padding:12px; font:inherit; }
  .btn { background:var(--accent); color:#fff; border:0; border-radius:6px; padding:8px 14px; cursor:pointer; font:inherit; }
  .ghost { background:#4e5058; } label { color:var(--muted); font-size:13px; } select { background:var(--input); color:var(--text); border:0; padding:6px; border-radius:4px; }
</style></head><body>
<nav>
  <h2>You are</h2><select id="me"></select>
  <h2 id="gname"></h2><button data-where="general"># general</button>
  <h2>Direct messages</h2><button data-where="dm">@ Surogate</button>
</nav>
<main>
  <header><strong id="title"></strong>
    <label id="mlabel"><input type="checkbox" id="mention" checked> @mention Surogate</label>
    <span style="flex:1"></span><button class="btn ghost" id="reconnect" title="send op 7 RECONNECT">Force reconnect</button></header>
  <div id="log"></div><div id="typing"></div>
  <form id="f"><input type="text" id="text" placeholder="Message" autocomplete="off" autofocus><button class="btn">Send</button></form>
</main>
<script>
const C = __CONFIG__;
const $ = (id) => document.getElementById(id);
let where = "dm";
for (const [k, u] of Object.entries(C.users)) $("me").add(new Option(u.global_name, k));
$("gname").textContent = C.guild;
const channel = () => where === "general" ? C.general
  : Object.entries(C.dms).find(([, k]) => k === $("me").value)[0];
function pick(w) {
  where = w;
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("on", b.dataset.where === w));
  $("title").textContent = w === "general" ? "# general" : "@ Surogate";
  $("mlabel").style.display = w === "general" ? "" : "none";
  render();
}
document.querySelectorAll("nav button").forEach(b => b.onclick = () => pick(b.dataset.where));
$("me").onchange = render;
const esc = (s) => s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const fmt = (s) => esc(s).replace(new RegExp("&lt;@!?" + C.bot + "&gt;", "g"), '<span class="mention">@Surogate</span>');
async function render() {
  const r = await fetch("/api/discord/messages?channel=" + channel());
  const {messages, typing} = await r.json();
  const log = $("log"); const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.innerHTML = messages.map(m => `<div class="m"><span class="who ${m.author.bot ? "bot" : ""}">${esc(m.author.global_name || m.author.username)}</span>`
    + `<span class="t">${m.timestamp.slice(11, 16)}</span><div class="body">${fmt(m.content)}`
    + (m.attachments || []).map(a => `<div>📎 ${esc(a.filename)} (${a.size} B)</div>`).join("")
    + `${m.edited_timestamp ? ' <span class="ed">(edited)</span>' : ""}</div></div>`).join("");
  if (atEnd) log.scrollTop = log.scrollHeight;
  $("typing").textContent = typing ? "Surogate is typing…" : "";
}
$("f").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("text").value.trim(); if (!text) return;
  $("text").value = "";
  await fetch("/api/discord/send", {method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({user: $("me").value, text, where, mention: where === "general" && $("mention").checked})});
  render();
};
$("reconnect").onclick = () => fetch("/api/discord/reconnect", {method: "POST"});
pick("dm"); setInterval(render, 1000);
</script></body></html>
"""


def main() -> None:
    global PORT, HEARTBEAT_MS, DENY_MESSAGE_CONTENT, STATE_FILE, _ids
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--state", default="", help="JSON file the message log is kept in across restarts")
    parser.add_argument("--heartbeat-ms", type=int, default=HEARTBEAT_MS, help="heartbeat_interval sent in HELLO")
    parser.add_argument("--deny-message-content", action="store_true",
                        help="close IDENTIFY asking for the Message Content intent with 4014")
    args = parser.parse_args()
    PORT, HEARTBEAT_MS, STATE_FILE = args.port, args.heartbeat_ms, args.state
    DENY_MESSAGE_CONTENT = args.deny_message_content
    if STATE_FILE and os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state.update(json.load(f))
        if state["messages"]:
            _ids = itertools.count(max(int(m["id"]) for m in state["messages"]) + 1)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
