"""Our Romanian TTS (``surogate serve --tts``, ``POST /v1/audio/speech``) as a LiveKit TTS.

One request per sentence (the agent's tts_node splits). The server rejects unknown fields, so the
body is exactly four keys; audio streams back as raw PCM at ``X-Audio-Sample-Rate``.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import httpx
from livekit import rtc
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, APIError, APIStatusError, tts, utils

from surogates.voice.audio import PhoneVoice


class RoTTS(tts.TTS):
    def __init__(self, *, url: str, voice: str = "female", sample_rate: int = 24000,
                 client: httpx.AsyncClient | None = None) -> None:
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=sample_rate, num_channels=1)
        self._url, self._voice = url, voice
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))

    @property
    def model(self) -> str:
        return "amami-110m-ro"

    @property
    def provider(self) -> str:
        return "surogate"

    def synthesize(self, text: str, *,
                   conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> RoChunkedStream:
        return RoChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        await self._client.aclose()


class RoChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        t: RoTTS = self._tts
        body = {"input": self._input_text, "voice": t._voice, "response_format": "pcm", "stream_format": "audio"}
        async with t._client.stream("POST", t._url, json=body) as r:
            if r.status_code != 200:
                raise APIStatusError(f"TTS {r.status_code}", status_code=r.status_code, body=(await r.aread()).decode())
            rate = int(r.headers.get("x-audio-sample-rate", t.sample_rate))
            if rate != t.sample_rate:
                raise APIError(f"TTS answered at {rate} Hz, expected {t.sample_rate}")
            output_emitter.initialize(request_id=utils.shortuuid(), sample_rate=rate, num_channels=1,
                                      mime_type="audio/pcm")
            voice = PhoneVoice(rate)
            async for chunk in r.aiter_bytes():
                if out := voice(chunk):
                    output_emitter.push(out)
            if out := voice(b"", last=True):
                output_emitter.push(out)
            output_emitter.flush()


class PhraseCache:
    """Fixed phrases (greeting, goodbye, sorry) as ready-to-play PCM in Redis.

    Every call starts with the same greeting, and a fresh synthesis is the TTS's first-audio delay
    plus its leading silence. Synthesized once (already shaped by PhoneVoice), a phrase then plays
    the moment the caller connects. Redis trouble falls back to synthesizing, never to silence.
    """

    TTL = 7 * 24 * 3600  # a greeting edited in Studio is a new key; old ones expire
    FRAME_S = 0.1

    def __init__(self, redis, tts: RoTTS) -> None:
        self._redis, self._tts = redis, tts

    def _key(self, text: str) -> str:
        t = self._tts
        return f"voice:phrase:{t.model}:{t._voice}:{t.sample_rate}:{hashlib.sha256(text.encode()).hexdigest()}"

    async def frames(self, text: str) -> AsyncIterator[rtc.AudioFrame]:
        key, pcm = self._key(text), None
        try:
            pcm = await self._redis.get(key)
        except Exception:
            pass
        if pcm is None:
            pcm = b"".join([ev.frame.data.tobytes() async for ev in self._tts.synthesize(text)])
            try:
                await self._redis.set(key, pcm, ex=self.TTL)
            except Exception:
                pass
        rate, step = self._tts.sample_rate, int(self._tts.sample_rate * self.FRAME_S) * 2
        for i in range(0, len(pcm), step):
            chunk = pcm[i:i + step]
            yield rtc.AudioFrame(chunk, rate, 1, len(chunk) // 2)
