"""A fake LiveKit for outbound calls, with the dialer running against it and a scripted callee.

It serves the two LiveKit calls the dialer makes (``AgentDispatchService/CreateDispatch``
and ``SIP/CreateSIPParticipant``), records every dial, and answers each number the
way you say: answered (default), busy, no answer, declined.  An answered call with a
script is then talked through in text: the agent's opening words, each scripted line
as the callee's, and the agent's answers, through the same CallSession the voice
worker drives (no audio, no LiveKit media).  So a step's call binds the step on the
first words, and an alert call is acknowledged by a "da".

Run it with the runtime's config (the database and Redis the agent uses):

    SUROGATES_CONFIG=~/.surogates/config.yaml .venv/bin/python scripts/fake_voice.py \\
        --outcome +40712000999=busy --script "+40712000103=Da|Mă simt bine, mulțumesc"

then give the agent's voice number an outbound trunk (routing config
``outbound_trunk_id``, or ``SUROGATES_VOICE_OUTBOUND_TRUNK_ID=ST_fake``) and a
person a granted ``voice`` identity.  ``GET http://127.0.0.1:58130/calls`` lists
what was dialled.  Do not run ``surogates voice`` at the same time: both would
dial the same rows.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from livekit import api

app = FastAPI()
CALLS: list[dict] = []
DISPATCHES: dict[str, str] = {}  # room -> job metadata
OUTCOMES: dict[str, str] = {}
SCRIPTS: dict[str, list[str]] = {}
SIP = {"busy": ("486", "Busy Here"), "no-answer": ("480", "Temporarily Unavailable"),
       "declined": ("603", "Decline")}


@app.post("/twirp/livekit.AgentDispatchService/CreateDispatch")
async def create_dispatch(request: Request) -> Response:
    req = api.CreateAgentDispatchRequest.FromString(await request.body())
    DISPATCHES[req.room] = req.metadata
    out = api.AgentDispatch(id=f"AD_{len(DISPATCHES)}", agent_name=req.agent_name, room=req.room,
                            metadata=req.metadata)
    return Response(out.SerializeToString(), media_type="application/protobuf")


@app.post("/twirp/livekit.SIP/CreateSIPParticipant")
async def create_sip_participant(request: Request) -> Response:
    req = api.CreateSIPParticipantRequest.FromString(await request.body())
    outcome = OUTCOMES.get(req.sip_call_to, "answered")
    CALLS.append({"at": time.strftime("%H:%M:%S"), "to": req.sip_call_to, "from": req.sip_number,
                  "trunk": req.sip_trunk_id, "room": req.room_name, "outcome": outcome})
    print(f"[fake-voice] dial {req.sip_call_to} from {req.sip_number} via {req.sip_trunk_id}: {outcome}")
    if outcome in SIP:
        code, reason = SIP[outcome]
        return JSONResponse({"code": "unavailable", "msg": reason,
                             "meta": {"sip_status_code": code, "sip_status": reason}}, status_code=400)
    if req.sip_call_to in SCRIPTS and req.room_name in DISPATCHES:
        asyncio.create_task(converse(DISPATCHES[req.room_name], SCRIPTS[req.sip_call_to]))
    out = api.SIPParticipantInfo(participant_id="PA_fake", participant_identity=req.participant_identity,
                                 room_name=req.room_name, sip_call_id=f"SCL_{len(CALLS)}")
    return Response(out.SerializeToString(), media_type="application/protobuf")


@app.post("/twirp/livekit.RoomService/DeleteRoom")
async def delete_room(request: Request) -> Response:
    return Response(api.DeleteRoomResponse().SerializeToString(), media_type="application/protobuf")


@app.get("/calls")
async def calls() -> list[dict]:
    return CALLS


async def converse(metadata: str, lines: list[str]) -> None:
    """The answered call, in text: what the voice worker does with each utterance, minus audio."""
    from redis.asyncio import Redis

    from surogates.channels.outbound import record_status
    from surogates.config import load_settings
    from surogates.db.engine import async_engine_from_settings, async_session_factory
    from surogates.session.store import SessionStore
    from surogates.voice.outbound import LIVE_GRACE_SECONDS, Outbound, acknowledges, mark_live
    from surogates.voice.sessions import VoiceSessions

    call_out = Outbound.from_metadata(metadata)
    settings = load_settings()
    engine = async_engine_from_settings(settings.db)
    sf, redis = async_session_factory(engine), Redis.from_url(settings.redis.url)
    try:
        sessions = VoiceSessions(store=SessionStore(sf, redis=redis), redis=redis, session_factory=sf)
        call = await sessions.join_call(uuid.UUID(call_out.session_id), call_id=call_out.call_id,
                                        callee=call_out.to, said=call_out.text)
        await mark_live(redis, call.session_id, 600)
        print(f"[fake-voice] agent: {call_out.text}")
        for line in lines:
            if call_out.kind == "alert" and acknowledges(line):
                for pid in call_out.provider_ids():
                    await record_status(sf, provider_message_id=pid, status="read")
                print("[fake-voice] alert acknowledged")
            print(f"[fake-voice] callee: {line}")
            after = await call.send(line)
            answer = "".join([chunk async for chunk in call.stream(after)])
            print(f"[fake-voice] agent: {answer}")
        await call.end()
        await mark_live(redis, call.session_id, LIVE_GRACE_SECONDS)
        print("[fake-voice] hung up")
    except Exception as exc:  # noqa: BLE001 — a fake: say what went wrong and carry on
        print(f"[fake-voice] conversation failed: {exc!r}")
    finally:
        await redis.aclose()
        await engine.dispose()


async def main(port: int) -> None:
    from surogates.config import load_settings
    from surogates.voice.outbound import dial_forever

    settings = load_settings()
    vs = settings.voice
    vs.livekit_url = f"http://127.0.0.1:{port}"
    vs.livekit_api_key, vs.livekit_api_secret = vs.livekit_api_key or "fake", vs.livekit_api_secret or "fake-secret"
    vs.outbound_trunk_id = vs.outbound_trunk_id or "ST_fake"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    print(f"[fake-voice] LiveKit on http://127.0.0.1:{port}; dialling the voice outbox")
    await asyncio.gather(server.serve(), dial_forever(settings))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=58130)
    parser.add_argument("--outcome", action="append", default=[], metavar="+NUMBER=answered|busy|no-answer|declined")
    parser.add_argument("--script", action="append", default=[], metavar="+NUMBER=line|line|…")
    args = parser.parse_args()
    for pair in args.outcome:
        number, _, outcome = pair.partition("=")
        OUTCOMES[number] = outcome
    for pair in args.script:
        number, _, lines = pair.partition("=")
        SCRIPTS[number] = [line for line in lines.split("|") if line]
    asyncio.run(main(args.port))
