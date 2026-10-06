"""The TTS plugin sends exactly what our server accepts and hands LiveKit trimmed, phone-ready audio."""
import json

import httpx
import numpy as np
import pytest
from livekit.agents import APIConnectOptions, APIError

from surogates.voice.tts import RoTTS

RATE = 24000


def _speech(lead: float) -> bytes:
    tone = (np.sin(np.arange(RATE // 2) * 2 * np.pi * 300 / RATE) * 8000).astype("<i2")
    return np.concatenate([np.zeros(int(lead * RATE), "<i2"), tone]).tobytes()


def _tts(handler, **kw) -> RoTTS:
    return RoTTS(url="http://tts/v1/audio/speech", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


async def test_synthesize_sends_the_accepted_body_and_trims_the_lead():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"x-audio-sample-rate": str(RATE)}, content=_speech(0.4))

    frames = [ev.frame async for ev in _tts(handler, voice="male").synthesize("Bună ziua.")]
    assert seen["body"] == {"input": "Bună ziua.", "voice": "male", "response_format": "pcm", "stream_format": "audio"}
    assert all(f.sample_rate == RATE and f.num_channels == 1 for f in frames)
    assert 0.5 <= sum(f.samples_per_channel for f in frames) / RATE < 0.7  # 0.5 s of voice; 0.4 s lead cut


async def test_a_different_sample_rate_is_an_error_not_chipmunk_audio():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-audio-sample-rate": "22050"}, content=_speech(0.0))

    with pytest.raises(APIError):
        async for _ in _tts(handler).synthesize("x", conn_options=APIConnectOptions(max_retry=0)):
            pass


async def test_fixed_phrases_are_synthesized_once_and_replayed_from_redis():
    """The greeting is the same on every call: synthesize it once, play it instantly afterwards."""
    from surogates.voice.tts import PhraseCache

    class _Redis(dict):
        async def get(self, k):
            return dict.get(self, k)

        async def set(self, k, v, ex=None):
            self[k] = v

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["input"])
        return httpx.Response(200, headers={"x-audio-sample-rate": str(RATE)}, content=_speech(0.4))

    redis, tts = _Redis(), _tts(handler, voice="male")
    first = [f async for f in PhraseCache(redis, tts).frames("Bună ziua!")]
    again = [f async for f in PhraseCache(redis, _tts(handler, voice="male")).frames("Bună ziua!")]
    assert calls == ["Bună ziua!"]  # the second call, a new process, read it back
    assert sum(f.samples_per_channel for f in again) == sum(f.samples_per_channel for f in first) > 0
    assert all(f.sample_rate == RATE for f in again)
