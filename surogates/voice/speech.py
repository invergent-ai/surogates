"""Build a line's hearing (STT) and speaking (TTS) from its routing settings: our Romanian models, or a
provider's LiveKit plugin with the owner's key. The catalog is ``surogates.voice.providers``.

Every provider speaks through the same pipeline: VoiceAgent.tts_node splits the answer into sentences
and synthesizes them one by one (barge-in, echo filtering and the pause between sentences depend on
it), and every voice goes through PhoneVoice, so the background sits at the same distance behind any
of them.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    stt,
    tts,
    utils,
)

from surogates.voice.audio import PhoneVoice
from surogates.voice.providers import PROVIDERS, SUROGATE
from surogates.voice.stt import RoSTT
from surogates.voice.tts import RoTTS


@dataclass(frozen=True)
class Slot:
    """Who hears or speaks for a line. ``key_ref`` names the owner's key in the vault (never the key)."""
    provider: str = SUROGATE
    model: str = ""
    voice: str = "female"
    key_ref: str = ""
    base_url: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_routing(cls, cfg: Any, voice: str = "female") -> Slot:
        """A slot from the routing row; a bad one is ours (a line with no slot predates providers)."""
        if not isinstance(cfg, dict) or cfg.get("provider") not in PROVIDERS or cfg["provider"] == SUROGATE:
            return cls(voice=voice if voice in ("female", "male") else "female")

        def text(key: str) -> str:
            v = cfg.get(key)
            return v.strip() if isinstance(v, str) else ""

        options = cfg.get("options")
        return cls(provider=cfg["provider"], model=text("model"), voice=text("voice"), key_ref=text("key_ref"),
                   base_url=text("base_url"), options=options if isinstance(options, dict) else {})

    @property
    def ours(self) -> bool:
        return self.provider == SUROGATE


# Turn-taking. Our STT ends an utterance itself after 640 ms of quiet, so its end ends the turn at once.
# Other providers endpoint differently (some not at all): the VAD decides, after half a second of quiet.
TURN_OURS = {"turn_detection": "stt", "endpointing": {"min_delay": 0.05, "max_delay": 1.5}}
TURN_PROVIDER = {"turn_detection": "vad", "endpointing": {"min_delay": 0.5, "max_delay": 1.5}}
INTERRUPTION = {
    "interruption": {"mode": "vad", "min_duration": 0.6, "min_words": 2,
                     "resume_false_interruption": True, "false_interruption_timeout": 1.5},
    "preemptive_generation": {"enabled": False},  # the agent runs tools: no speculative turns
}


def turn_handling(hearing: Slot) -> dict:
    return {**(TURN_OURS if hearing.ours else TURN_PROVIDER), **INTERRUPTION}


def build_stt(slot: Slot, *, key: str, language: str, stt_url: str) -> stt.STT:
    p, m = slot.provider, slot.model or (PROVIDERS[slot.provider].stt[0].id if PROVIDERS[slot.provider].stt else "")
    if slot.ours:
        return RoSTT(url=stt_url)
    if p == "elevenlabs":
        from livekit.plugins import elevenlabs
        return elevenlabs.STT(api_key=key, model=m, language_code=language,
                              server_vad={"vad_silence_threshold_secs": 0.6, "min_silence_duration_ms": 600},
                              tag_audio_events=False)
    if p == "cartesia":
        from livekit.plugins import cartesia
        return cartesia.STT(api_key=key, model=m, language=language)
    if p == "deepgram":
        from livekit.plugins import deepgram
        return deepgram.STT(api_key=key, model=m, language=language, punctuate=True, smart_format=True,
                            filler_words=False, endpointing_ms=300)
    if p == "openai":
        from livekit.plugins import openai
        return openai.STT(api_key=key, model=m, language=language, use_realtime=True)
    if p == "gradium":
        from livekit.plugins import gradium
        return gradium.STT(api_key=key, model_name=m or "default", language=language)
    if p == "openai_compat":
        from livekit.plugins import openai
        return openai.STT(api_key=key or "none", base_url=slot.base_url, model=m, language=language)
    raise ValueError(f"{p} cannot hear")


def build_tts(slot: Slot, *, key: str, language: str, tts_url: str) -> tts.TTS:
    p, m = slot.provider, slot.model or (PROVIDERS[slot.provider].tts[0].id if PROVIDERS[slot.provider].tts else "")
    o = slot.options
    if slot.ours:
        return RoTTS(url=tts_url, voice=slot.voice)
    if p == "elevenlabs":
        from livekit.plugins import elevenlabs
        inner = elevenlabs.TTS(api_key=key, voice_id=slot.voice, model=m, language=language, encoding="pcm_16000",
                               voice_settings=_eleven_settings(o))
    elif p == "cartesia":
        from livekit.plugins import cartesia
        inner = cartesia.TTS(api_key=key, model=m, voice=slot.voice, language=language, speed=o.get("speed"),
                             sample_rate=16000, word_timestamps=False)
    elif p == "deepgram":
        from livekit.plugins import deepgram
        inner = deepgram.TTS(api_key=key, model=slot.voice, sample_rate=16000, speed=o.get("speed"))
    elif p == "openai":
        from livekit.plugins import openai
        inner = openai.TTS(api_key=key, model=m, voice=slot.voice, speed=float(o.get("speed") or 1.0),
                           instructions=o.get("instructions") or NOT_GIVEN)
    elif p == "gradium":
        from livekit.plugins import gradium
        inner = gradium.TTS(api_key=key, model_name=m or "default", voice_id=slot.voice)
    elif p == "fishaudio":
        from livekit.plugins import fishaudio
        inner = fishaudio.TTS(api_key=key, model=m, voice_id=slot.voice, output_format="pcm", sample_rate=16000,
                              latency_mode="balanced")
    elif p == "openai_compat":
        from livekit.plugins import openai
        inner = openai.TTS(api_key=key or "none", base_url=slot.base_url, model=m, voice=slot.voice,
                           response_format="pcm")
    else:
        raise ValueError(f"{p} cannot speak")
    return ShapedTTS(inner, provider=p, model=m, voice=slot.voice)


def _eleven_settings(o: Mapping[str, Any]):
    from livekit.plugins import elevenlabs
    if not any(k in o for k in ("stability", "similarity", "speed")):
        return NOT_GIVEN
    return elevenlabs.VoiceSettings(stability=float(o.get("stability", 0.5)),
                                    similarity_boost=float(o.get("similarity", 0.75)),
                                    speed=float(o.get("speed", 1.0)))


# How much each provider's voice is lifted before the soft limiter, so it lands as loud as ours (which
# PhoneVoice lifts 10 dB; about -16 dBFS while speaking). ponytail: one fixed gain per provider. Measured
# 2026-10-07 for ElevenLabs (three Romanian library voices, -20 to -14 dBFS with +4 dB); the others start
# at the same +4 until scripts/voice-dev/providers_smoke.py measures them. A voice far off its
# provider's usual would need a level that adapts per call instead.
GAIN_DB = {"elevenlabs": 4.0}
DEFAULT_GAIN_DB = 4.0


def phrase_scope(slot: Slot, language: str, org_id: str) -> str:
    """Whose cached phrases a line plays (PhraseCache's scope). Our voices are shared by every line; a
    provider's belong to the org that paid for them, and change with anything else that shapes the audio
    besides the model and voice the cache key already has (options, server, language, our gain)."""
    if slot.ours:
        return ""
    shape = [dict(slot.options), slot.base_url, language, GAIN_DB.get(slot.provider, DEFAULT_GAIN_DB)]
    return f"{org_id}:{hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:8]}"


class ShapedTTS(tts.TTS):
    """A provider's TTS, one sentence at a time, shaped like ours for a phone line (PhoneVoice)."""

    def __init__(self, inner: tts.TTS, *, provider: str, model: str, voice: str) -> None:
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=inner.sample_rate,
                         num_channels=1)
        self._inner, self._provider, self._model, self._voice = inner, provider, model, voice

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

    def synthesize(self, text: str, *,
                   conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> ShapedStream:
        return ShapedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        await self._inner.aclose()


class ShapedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        t: ShapedTTS = self._tts
        voice = PhoneVoice(t.sample_rate, gain_db=GAIN_DB.get(t.provider, DEFAULT_GAIN_DB))
        output_emitter.initialize(request_id=utils.shortuuid(), sample_rate=t.sample_rate, num_channels=1,
                                  mime_type="audio/pcm")
        # this stream retries the sentence; the provider inside must not as well (4 x 4 tries is minutes of
        # silence before a dead provider is noticed)
        once = dataclasses.replace(self._conn_options, max_retry=0)
        async with t._inner.synthesize(self._input_text, conn_options=once) as stream:
            async for ev in stream:
                frame = ev.frame
                if frame.sample_rate != t.sample_rate or frame.num_channels != 1:
                    raise ValueError(f"{t.provider} answered {frame.sample_rate} Hz x{frame.num_channels}")
                if out := voice(frame.data.tobytes()):
                    output_emitter.push(out)
        if out := voice(b"", last=True):
            output_emitter.push(out)
        output_emitter.flush()
