"""A scripted phone caller for the local loop: what a softphone does, plus timings and transcripts.

Joins a LiveKit room as the SIP participant would (same ``sip.*`` attributes), dispatches the
voice agent, says each line with our Romanian TTS, and reports for every turn how long the agent
took to start speaking after the caller stopped, and what it said (transcribed by our STT).

    .venv/bin/python scripts/voice-dev/caller.py "Bună ziua, cu ce mă poți ajuta?" "Mulțumesc, atât."

Needs the local LiveKit (``livekit-up.sh``), ``surogates voice`` running, and the speech tunnel.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

import httpx
import numpy as np
import soxr
from livekit import api, rtc
from websockets.asyncio.client import connect

URL, KEY, SECRET = "ws://127.0.0.1:7880", "devkey", "secret"
TTS, STT = "http://127.0.0.1:18080/v1/audio/speech", "ws://127.0.0.1:18001/v1/audio/streams"
RATE = 24000
FRAME = RATE // 50  # 20 ms
QUIET_END = 1.5  # seconds of agent silence that end its reply
LOUD = 300  # int16 RMS above which a frame is speech (the typing sound is quieter)


async def speech(text: str) -> np.ndarray:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(TTS, json={"input": text, "voice": "male", "response_format": "pcm", "stream_format": "audio"})
        r.raise_for_status()
        return np.frombuffer(r.content, "<i2")


async def transcribe(pcm: np.ndarray) -> str:
    if not len(pcm):
        return ""
    x = soxr.resample(pcm, RATE, 16000).astype("<i2").tobytes()
    finals = []
    async with connect(STT, max_size=None) as ws:
        await ws.recv()
        for i in range(0, len(x), 32000):
            await ws.send(x[i:i + 32000])
        await ws.send(b"\0" * 32000)
        await ws.send(json.dumps({"type": "finish"}))
        async for m in ws:
            ev = json.loads(m)
            if ev["type"] == "final" and ev.get("text"):
                finals.append(ev["text"])
            if ev["type"] == "done":
                break
    return " ".join(finals)


class Ear:
    """Records the agent's audio and when it was loud."""

    def __init__(self) -> None:
        self.frames: list[np.ndarray] = []
        self.loud_at: list[float] = []

    async def listen(self, track: rtc.Track) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=RATE, num_channels=1):
            x = np.frombuffer(ev.frame.data, "<i2")
            self.frames.append(x)
            if np.sqrt(np.mean(x.astype(np.float32) ** 2)) > LOUD:
                self.loud_at.append(time.monotonic())

    async def reply(self, since: float, timeout: float = 30.0) -> tuple[float | None, np.ndarray]:
        """Wait for the agent to start and finish speaking after ``since``; (delay, audio)."""
        start_n, deadline = len(self.frames), since + timeout
        while time.monotonic() < deadline:
            loud = [t for t in self.loud_at if t > since]
            if loud and time.monotonic() - loud[-1] > QUIET_END:
                return loud[0] - since, np.concatenate(self.frames[start_n:])
            await asyncio.sleep(0.05)
        loud = [t for t in self.loud_at if t > since]
        return (loud[0] - since if loud else None), np.concatenate(self.frames[start_n:] or [np.zeros(0, "<i2")])


async def say(source: rtc.AudioSource, pcm: np.ndarray) -> None:
    pcm = np.concatenate([pcm, np.zeros(RATE // 2, "<i2")])
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        await source.capture_frame(rtc.AudioFrame(pcm[i:i + FRAME].tobytes(), RATE, 1, FRAME))


async def main(lines: list[str], called: str, caller: str) -> None:
    room_name = f"call-test-{uuid.uuid4().hex[:6]}"
    async with api.LiveKitAPI(URL.replace("ws", "http"), KEY, SECRET) as lk:
        await lk.room.create_room(api.CreateRoomRequest(name=room_name))
        await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(agent_name="surogate-voice", room=room_name))
    token = (api.AccessToken(KEY, SECRET).with_identity(f"sip_{caller}").with_kind("sip")
             .with_attributes({"sip.trunkPhoneNumber": called, "sip.phoneNumber": caller, "sip.callID": room_name})
             .with_grants(api.VideoGrants(room_join=True, room=room_name)).to_jwt())
    room, ear, ended = rtc.Room(), Ear(), asyncio.Event()

    @room.on("track_subscribed")
    def _track(track, publication, _participant):
        # the agent's voice only: its background track (typing while it works) is not the reply
        if track.kind == rtc.TrackKind.KIND_AUDIO and publication.name != "background_audio":
            asyncio.ensure_future(ear.listen(track))

    room.on("disconnected", lambda *_: ended.set())
    await room.connect(URL, token)
    source = rtc.AudioSource(RATE, 1)
    await room.local_participant.publish_track(rtc.LocalAudioTrack.create_audio_track("mic", source),
                                               rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    t0 = time.monotonic()
    print(f"room {room_name}: calling {called} as {caller}")
    delay, audio = await ear.reply(t0)
    print(f"greeting after {delay if delay is None else round(delay, 2)} s: {await transcribe(audio)!r}")
    for line in lines:
        pcm = await speech(line)
        await say(source, pcm)
        stopped = time.monotonic() - 0.5  # the half second of trailing silence in say()
        print(f"\ncaller: {line!r}")
        delay, audio = await ear.reply(stopped)
        print(f"agent after {delay if delay is None else round(delay, 2)} s: {await transcribe(audio)!r}")
    try:
        await asyncio.wait_for(ended.wait(), 8)
        print("\nthe agent hung up")
    except asyncio.TimeoutError:
        print("\ncall still open; hanging up")
    await room.disconnect()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("lines", nargs="+")
    p.add_argument("--called", default="+40300000001")
    p.add_argument("--caller", default="+40722000111")
    a = p.parse_args()
    asyncio.run(main(a.lines, a.called, a.caller))
