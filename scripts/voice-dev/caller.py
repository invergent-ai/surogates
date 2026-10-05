"""Scripted phone calls for the local loop: what a softphone does, plus timings and transcripts.

A ``Call`` joins a LiveKit room as the SIP participant would (same ``sip.*`` attributes), dispatches
the voice agent, keeps a microphone open like a phone line (silence between words), says lines with
our Romanian TTS and hears the agent's voice track (the background typing track is ignored).
``scenarios.py`` builds on it. As a command it places one call:

    .venv/bin/python scripts/voice-dev/caller.py "Bună ziua. Ce poți face pentru mine?" "Mulțumesc, atât."

Needs the local LiveKit (``livekit-up.sh``), ``surogates voice`` running, and the speech tunnel.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass

import httpx
import numpy as np
import soxr
from livekit import api, rtc
from websockets.asyncio.client import connect

URL, KEY, SECRET = "ws://127.0.0.1:7880", "devkey", "secret"  # local LiveKit's dev keys
TTS, STT = "http://127.0.0.1:18080/v1/audio/speech", "ws://127.0.0.1:18001/v1/audio/streams"
DID, CALLER = "+40300000001", "+40722000111"
RATE = 24000
FRAME = RATE // 50  # 20 ms
QUIET_END = 1.5  # seconds of agent silence that end its reply
LOUD = 300  # int16 RMS above which a frame is speech


async def speech(text: str, voice: str = "male") -> np.ndarray:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(TTS, json={"input": text, "voice": voice, "response_format": "pcm", "stream_format": "audio"})
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


@dataclass
class Reply:
    delay: float | None  # seconds from the caller's last word to the agent's first; None: it never spoke
    text: str
    seconds: float  # how long the agent spoke


class Call:
    def __init__(self, called: str = DID, caller: str = CALLER) -> None:
        self.called, self.caller = called, caller
        self.room_name = f"call-qa-{uuid.uuid4().hex[:8]}"
        self.heard: list[tuple[float, np.ndarray, bool]] = []  # (when, frame, loud) of the agent's voice
        self.ended = asyncio.Event()
        self._room = rtc.Room()
        self._outbox: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._said = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> Call:
        await self.dial()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.hang_up()

    async def dial(self) -> None:
        async with api.LiveKitAPI(URL.replace("ws", "http"), KEY, SECRET) as lk:
            await lk.room.create_room(api.CreateRoomRequest(name=self.room_name))
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name="surogate-voice", room=self.room_name))
        token = (api.AccessToken(KEY, SECRET).with_identity(f"sip_{self.caller}").with_kind("sip")
                 .with_attributes({"sip.trunkPhoneNumber": self.called, "sip.phoneNumber": self.caller,
                                   "sip.callID": self.room_name})
                 .with_grants(api.VideoGrants(room_join=True, room=self.room_name)).to_jwt())

        @self._room.on("track_subscribed")
        def _track(track, publication, _participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO and publication.name != "background_audio":
                self._tasks.append(asyncio.ensure_future(self._listen(track)))

        self._room.on("disconnected", lambda *_: self.ended.set())
        self.dialed_at = time.monotonic()
        await self._room.connect(URL, token)
        source = rtc.AudioSource(RATE, 1)
        await self._room.local_participant.publish_track(
            rtc.LocalAudioTrack.create_audio_track("mic", source),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        self._tasks.append(asyncio.ensure_future(self._mic(source)))

    async def _mic(self, source: rtc.AudioSource) -> None:
        """A phone line never goes quiet on the wire: silence between the lines we say."""
        silence = np.zeros(FRAME, "<i2")
        while True:
            pcm = self._outbox.get_nowait() if not self._outbox.empty() else None
            frames = [pcm[i:i + FRAME] for i in range(0, len(pcm) - FRAME + 1, FRAME)] if pcm is not None else [silence]
            for f in frames:
                await source.capture_frame(rtc.AudioFrame(f.tobytes(), RATE, 1, FRAME))
            if pcm is not None:
                self.stopped_at = time.monotonic()
                self._said.set()

    async def _listen(self, track: rtc.Track) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=RATE, num_channels=1):
            x = np.frombuffer(ev.frame.data, "<i2")
            self.heard.append((time.monotonic(), x, float(np.sqrt(np.mean(x.astype(np.float32) ** 2))) > LOUD))

    async def speak(self, text: str) -> float:
        """Say a line; returns when the last word left the microphone."""
        self._said.clear()
        await self._outbox.put(await speech(text))
        await self._said.wait()
        return self.stopped_at

    def first_loud_after(self, t: float) -> float | None:
        return next((w for w, _, loud in self.heard if loud and w > t), None)

    def quiet_from(self, t: float, window: float = 0.3) -> float | None:
        """The first moment after ``t`` from which the agent stayed silent for ``window`` seconds."""
        loud = [w for w, _, l in self.heard if l and w > t]
        start = t
        for w in loud:
            if w - start >= window:
                return start
            start = w
        return start if time.monotonic() - start >= window else None

    async def listen(self, since: float, timeout: float = 30.0) -> Reply:
        """Wait for the agent to start and finish speaking after ``since``."""
        deadline = since + timeout
        while time.monotonic() < deadline and not self.ended.is_set():
            loud = [w for w, _, l in self.heard if l and w > since]
            if loud and time.monotonic() - loud[-1] > QUIET_END:
                break
            await asyncio.sleep(0.05)
        loud = [w for w, _, l in self.heard if l and w > since]
        if not loud:
            return Reply(delay=None, text="", seconds=0.0)
        audio = [f for w, f, _ in self.heard if loud[0] - 0.1 <= w <= loud[-1] + 0.2]
        return Reply(delay=loud[0] - since, text=await transcribe(np.concatenate(audio)), seconds=loud[-1] - loud[0])

    async def ask(self, text: str, timeout: float = 30.0) -> Reply:
        return await self.listen(await self.speak(text), timeout)

    async def wait_hung_up(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.ended.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def hang_up(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self._room.disconnect()


def _secs(x: float | None) -> str:
    return "never" if x is None else f"{x:.2f} s"


async def main(lines: list[str], called: str, caller: str) -> None:
    async with Call(called, caller) as call:
        print(f"room {call.room_name}: calling {called} as {caller}")
        greeting = await call.listen(call.dialed_at)
        print(f"greeting after {_secs(greeting.delay)}: {greeting.text!r}")
        for line in lines:
            reply = await call.ask(line)
            print(f"\ncaller: {line!r}\nagent after {_secs(reply.delay)}: {reply.text!r}")
        print("\nthe agent hung up" if await call.wait_hung_up(8) else "\ncall still open; hanging up")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("lines", nargs="+")
    p.add_argument("--called", default=DID)
    p.add_argument("--caller", default=CALLER)
    a = p.parse_args()
    asyncio.run(main(a.lines, a.called, a.caller))
