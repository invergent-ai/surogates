"""The voice channel's behaviour, call by call: scripted calls with pass/fail checks and timings.

Each scenario places a call (``caller.Call``), checks what the caller heard, then checks what the
agent's session recorded (events in the local surogates DB) and where each turn's time went:

    .venv/bin/python scripts/voice-dev/scenarios.py              # the fast ones (~4 min)
    .venv/bin/python scripts/voice-dev/scenarios.py --all        # + silence and time limit (~3 min more)
    .venv/bin/python scripts/voice-dev/scenarios.py barge_in     # just these

Results go to ~/.surogate/voice-qa/<time>.json, and the run is compared with the previous one.
Local only: it refuses to run unless both databases in $SUROGATES_CONFIG are on this machine,
because the time-limit scenario edits the test number's routing row.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import yaml
from redis.asyncio import Redis

sys.path.insert(0, str(Path(__file__).parent))
from caller import DID, Call, Reply  # noqa: E402
from surogates.voice.text import fold

UNROUTED = "+40371000000"
OUT = Path.home() / ".surogate" / "voice-qa"


plain = fold  # STT spelling varies, the words do not


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    scenario: str
    room: str
    checks: list[Check] = field(default_factory=list)
    replies: list[dict] = field(default_factory=list)  # what the caller heard, with delays
    turns: list[dict] = field(default_factory=list)  # per user.message: harness wake, first token, answer
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and all(c.ok for c in self.checks)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    def heard(self, line: str, reply: Reply) -> Reply:
        self.replies.append({"caller": line, "delay": reply.delay, "agent": reply.text, "spoke_s": round(reply.seconds, 1)})
        return reply


class DB:
    """The local surogates and ops databases, from $SUROGATES_CONFIG. Refuses anything not on this machine."""

    def __init__(self) -> None:
        cfg = yaml.safe_load(open(os.path.expanduser(os.environ["SUROGATES_CONFIG"])))
        self.urls = {k: cfg[k]["url"].replace("+asyncpg", "") for k in ("db", "ops_db")}
        self.urls["redis"] = cfg["redis"]["url"]
        for k, u in self.urls.items():
            if urlparse(u).hostname not in ("127.0.0.1", "localhost"):
                sys.exit(f"refusing: {k} is not a local database (this suite edits routing rows)")

    async def events(self, room: str) -> list[asyncpg.Record]:
        conn = await asyncpg.connect(self.urls["db"])
        try:
            return await conn.fetch(
                "SELECT e.id, e.type, e.data, e.created_at FROM events e JOIN sessions s ON s.id = e.session_id "
                "WHERE s.config->>'voice_call_id' = $1 ORDER BY e.id", room)
        finally:
            await conn.close()

    async def set_routing(self, key: str, value) -> None:
        conn = await asyncpg.connect(self.urls["ops_db"])
        try:
            if value is None:
                await conn.execute("UPDATE channel_routing SET config = config - $1 "
                                   "WHERE channel_kind = 'voice' AND channel_identifier = $2", key, DID)
            else:
                await conn.execute("UPDATE channel_routing SET config = config || jsonb_build_object($1::text, $2::jsonb) "
                                   "WHERE channel_kind = 'voice' AND channel_identifier = $3", key, json.dumps(value), DID)
        finally:
            await conn.close()


def data(e) -> dict:
    d = e["data"]
    return json.loads(d) if isinstance(d, str) else (d or {})


def turns(events) -> list[dict]:
    """Where each turn's time went, from the session's own events."""
    out = []
    for i, u in enumerate(events):
        if u["type"] != "user.message":
            continue
        after = events[i + 1:]

        def first(pred):
            e = next((e for e in after if pred(e)), None)
            return round((e["created_at"] - u["created_at"]).total_seconds(), 2) if e else None

        out.append({"said": data(u).get("content", "")[-60:],
                    "wake_s": first(lambda e: e["type"] == "llm.request"),
                    # spoken words only: reasoning deltas are never heard
                    "first_token_s": first(lambda e: e["type"] == "llm.delta" and bool(data(e).get("content"))),
                    "answer_s": first(lambda e: e["type"] == "llm.response" and not (data(e).get("message") or {}).get("tool_calls"))})
    return out


# --- scenarios: each gets a dialled call and its Result -------------------------------------------

async def greeting(call: Call, r: Result) -> None:
    g = r.heard("(dial)", await call.listen(call.dialed_at))
    r.check("greeting heard", "buna ziua" in plain(g.text), g.text)
    r.check("greeting within 4 s of answering", g.delay is not None and g.delay < 4, f"{g.delay}")


async def question(call: Call, r: Result) -> None:
    await call.listen(call.dialed_at)
    a = r.heard("Cât face doi plus doi?", await call.ask("Cât face doi plus doi?"))
    r.check("answers 4", "patru" in plain(a.text) or "4" in a.text, a.text)


async def tool_question(call: Call, r: Result) -> None:
    await call.listen(call.dialed_at)
    line = "Caută pe internet cât este cursul euro astăzi."
    first = r.heard(line, await call.ask(line, timeout=60))
    r.check("something is said while the search runs (within 7 s)", first.delay is not None and first.delay < 7,
            f"{first.delay} s: {first.text}")
    answer = first
    if not any(c.isdigit() for c in first.text):  # that was the "O clipă, verific": the answer follows
        answer = r.heard("(the search)", await call.listen(time.monotonic(), timeout=60))
    r.check("answers with the rate", any(c.isdigit() for c in answer.text) or "lei" in plain(answer.text), answer.text)
    r.check("used a tool", any(e["type"] == "tool.call" for e in await DB().events(call.room_name)))


async def agent_asks(call: Call, r: Result) -> None:
    await call.listen(call.dialed_at)
    q = r.heard("Vreau să fac o programare.", await call.ask("Vreau să fac o programare."))
    asked = [e for e in await DB().events(call.room_name)
             if e["type"] == "tool.call" and data(e).get("name") == "ask_user_question"]
    r.check("agent asked through ask_user_question", bool(asked))
    r.check("the question was spoken", bool(q.text), q.text)
    a = r.heard("Marți.", await call.ask("Marți."))
    r.check("the answer moved the conversation on", bool(a.text), a.text)


async def barge_in(call: Call, r: Result) -> None:
    await call.listen(call.dialed_at)
    asked = await call.speak("Povestește-mi pe larg istoria orașului București, în cel puțin zece propoziții.")
    started = await call.wait_loud_after(asked)
    if not r.check("agent started the story", started is not None):
        return
    await asyncio.sleep(2.0)
    stopped = await call.speak("Stop. Spune-mi doar în ce an a devenit Bucureștiul capitală.")
    cut_at = call.started_at  # the caller's first word, not the start of synthesising it (~2 s over the tunnel)
    quiet = call.quiet_from(cut_at)
    r.check("agent stopped within 1.5 s of being talked over", quiet is not None and quiet - cut_at <= 1.5,
            f"{None if quiet is None else round(quiet - cut_at, 2)} s")
    a = r.heard("Stop. …în ce an…", await call.listen(stopped, timeout=40))
    r.check("answers the new question", "1659" in a.text or "1862" in a.text or "o mie sase sute" in plain(a.text), a.text)
    ev = await DB().events(call.room_name)
    users = [data(e).get("content", "") for e in ev if e["type"] == "user.message"]
    heard_reply = any(data(e).get("synthetic") == "voice_heard" for e in ev)
    noted = any("te-a întrerupt" in u for u in users[1:])
    r.check("history says what the caller heard", heard_reply or noted,
            "synthetic reply" if heard_reply else "note on next message" if noted else "neither")


async def backchannel(call: Call, r: Result) -> None:
    await call.listen(call.dialed_at)
    asked = await call.speak("Spune-mi trei lucruri interesante despre Cluj.")
    started = await call.wait_loud_after(asked)
    if not r.check("agent started answering", started is not None):
        return
    await asyncio.sleep(1.5)
    await call.speak("Da.")
    await asyncio.sleep(1.5)
    # Sound alone cannot tell: between a lookup's "O clipă…" and its typing the agent is silent and
    # working. What matters is in the session: the "Da" became no turn and cut no reply.
    ev = await DB().events(call.room_name)
    users = [data(e).get("content", "") for e in ev if e["type"] == "user.message"]
    cut = any(data(e).get("synthetic") == "voice_heard" for e in ev) or any("te-a întrerupt" in u for u in users)
    r.check("a short 'da' does not stop the agent", len(users) == 1 and not cut,
            f"{len(users)} caller turns, {'a reply was cut' if cut else 'nothing cut'}")


async def goodbye(call: Call, r: Result) -> None:
    await call.listen(call.dialed_at)
    a = r.heard("Mulțumesc, atât.", await call.ask("Mulțumesc, atât."))
    r.check("says goodbye", bool(a.text), a.text)
    r.check("hangs up after the goodbye", await call.wait_hung_up(10))


async def unrouted(call: Call, r: Result) -> None:
    g = r.heard("(dial)", await call.listen(call.dialed_at))
    r.check("says the number is not available", "nu este disponibil" in plain(g.text), g.text)
    r.check("hangs up", await call.wait_hung_up(10))
    r.check("no session was created", not await DB().events(call.room_name))


async def busy(call: Call, r: Result) -> None:
    g = r.heard("(dial)", await call.listen(call.dialed_at))
    r.check("says all lines are busy", "ocupate" in plain(g.text), g.text)
    r.check("hangs up", await call.wait_hung_up(10))
    r.check("no session was created", not await DB().events(call.room_name))


async def silence(call: Call, r: Result) -> None:
    await call.listen(call.dialed_at)
    t = time.monotonic()
    ask = r.heard("(silence)", await call.listen(t, timeout=45))
    r.check("asks if the caller is still there", "acolo" in plain(ask.text), ask.text)
    r.check("hangs up after more silence", await call.wait_hung_up(40))


async def time_limit(call: Call, r: Result) -> None:
    await call.listen(call.dialed_at)
    await call.speak("Povestește-mi pe larg istoria României, de la daci până azi, cât mai detaliat.")
    ended = await call.wait_hung_up(60)
    ev = await DB().events(call.room_name)
    # a refused call ("all lines busy") also hangs up: the limit only counts after a real conversation
    r.check("the call got a conversation", any(e["type"] == "user.message" for e in ev), f"{len(ev)} events")
    r.check("the call ends at the limit", ended,
            f"{round(time.monotonic() - call.dialed_at, 1)} s after dialling (limit 30 s)")


SCENARIOS = {  # name: (scenario, slow, called number)
    "greeting": (greeting, False, DID), "question": (question, False, DID),
    "tool_question": (tool_question, False, DID), "agent_asks": (agent_asks, False, DID),
    "barge_in": (barge_in, False, DID), "backchannel": (backchannel, False, DID),
    "goodbye": (goodbye, False, DID), "unrouted": (unrouted, False, UNROUTED), "busy": (busy, False, DID),
    "silence": (silence, True, DID), "time_limit": (time_limit, True, DID),
}


async def run(name: str) -> Result:
    fn, _, called = SCENARIOS[name]
    db = DB()
    if name == "time_limit":
        await db.set_routing("max_call_seconds", 30)
        await asyncio.sleep(31)  # the worker's routing cache keeps a row 30 s
    fillers = [f"qa-filler-{i}" for i in range(64)]
    if name == "busy":  # every line taken (the worker's default capacity is 8; 64 covers any setting)
        redis = Redis.from_url(db.urls["redis"])
        await redis.zadd("voice:call_slots", {f: time.time() + 120 for f in fillers})
    call = Call(called=called)
    r = Result(scenario=name, room=call.room_name)
    try:
        async with call:
            await asyncio.wait_for(fn(call, r), 240)
    except Exception as e:  # a scenario that crashes is a failed scenario, not a dead suite
        r.error = f"{type(e).__name__}: {e}"
    finally:
        if name == "time_limit":
            await db.set_routing("max_call_seconds", None)
        if name == "busy":
            await redis.zrem("voice:call_slots", *fillers)
            await redis.aclose()
    r.turns = turns(await db.events(call.room_name))
    return r


def report(results: list[Result], previous: dict | None) -> None:
    for r in results:
        print(f"\n{'PASS' if r.ok else 'FAIL'}  {r.scenario}  ({r.room})")
        for c in r.checks:
            print(f"   {'ok ' if c.ok else 'NO '} {c.name}" + (f" — {c.detail[:110]}" if c.detail else ""))
        if r.error:
            print(f"   ERR {r.error}")
        for t in r.turns:
            was = (previous or {}).get(r.scenario, {})
            print(f"   turn: wake {t['wake_s']} s, first token {t['first_token_s']} s, answer {t['answer_s']} s"
                  + (f"   (last run first token {was.get('first_token_s')} s)" if was else "")
                  + f"  «{t['said'][-40:]}»")
        for h in r.replies:
            if h["delay"] is not None:
                print(f"   caller heard the agent {h['delay']:.2f} s after «{h['caller'][:30]}»")
    print(f"\n{sum(r.ok for r in results)}/{len(results)} scenarios passed")


async def main(names: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    runs = sorted(OUT.glob("*.json"))
    previous = None
    if runs:
        prev = json.loads(runs[-1].read_text())
        previous = {r["scenario"]: (r["turns"][0] if r["turns"] else {}) for r in prev["results"]}
    results = []
    for name in names:
        print(f"... {name}", flush=True)
        results.append(await run(name))
    (OUT / time.strftime("%Y%m%d-%H%M%S.json")).write_text(
        json.dumps({"results": [asdict(r) | {"ok": r.ok} for r in results]}, ensure_ascii=False, indent=1, default=str))
    report(results, previous)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("names", nargs="*", help=f"scenarios to run: {', '.join(SCENARIOS)}")
    p.add_argument("--all", action="store_true", help="include the slow ones (silence, time limit)")
    a = p.parse_args()
    for n in a.names:
        if n not in SCENARIOS:
            sys.exit(f"unknown scenario {n!r}; known: {', '.join(SCENARIOS)}")
    asyncio.run(main(a.names or [n for n, (_, slow, _) in SCENARIOS.items() if a.all or not slow]))
    sys.stdout.flush()
    os._exit(0)  # LiveKit's FFI threads keep the interpreter alive after the last call
