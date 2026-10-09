"""Try every speech provider whose key is in the environment, the way a call would use it.

    ELEVENLABS_API_KEY=… CARTESIA_API_KEY=… .venv/bin/python scripts/voice-dev/providers_smoke.py [language]

For each provider: check the key, list three voices, speak one sentence with the first voice (through
the same ShapedTTS a call uses), then hear it back with the provider's STT when it has one. Prints the
time to the first audio and the voice's level, the number GAIN_DB in surogates/voice/speech.py is
tuned from (ours, after PhoneVoice, is about -20 dBFS). Spends a few cents per provider.
OPENAI_COMPAT_URL (+ optional OPENAI_COMPAT_KEY, _STT_MODEL, _TTS_MODEL, _VOICE) tries your own server.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx
import numpy as np
from livekit import rtc
from livekit.agents import stt as lk_stt
from livekit.agents.utils import http_context

from surogates.voice.providers import PROVIDERS, Conn, check, list_voices
from surogates.voice.speech import Slot, build_stt, build_tts

SENTENCES = {"ro": "Bună ziua, cu ce vă pot ajuta astăzi?", "en": "Hello, how can I help you today?"}
ENV = {"elevenlabs": "ELEVENLABS_API_KEY", "cartesia": "CARTESIA_API_KEY", "deepgram": "DEEPGRAM_API_KEY",
       "openai": "OPENAI_API_KEY", "gradium": "GRADIUM_API_KEY", "fishaudio": "FISH_API_KEY"}


async def speak(slot: Slot, key: str, language: str) -> tuple[float, np.ndarray, int]:
    tts = build_tts(slot, key=key, language=language, tts_url="")
    began, first, frames = time.monotonic(), None, []
    async with tts.synthesize(SENTENCES.get(language, SENTENCES["en"])) as stream:
        async for ev in stream:
            first = first or time.monotonic() - began
            frames.append(np.frombuffer(ev.frame.data, "<i2"))
    await tts.aclose()
    return first or 0.0, np.concatenate(frames) if frames else np.zeros(0, "<i2"), tts.sample_rate


async def hear(slot: Slot, key: str, language: str, pcm: np.ndarray, rate: int) -> str:
    stt = build_stt(slot, key=key, language=language, stt_url="")
    if not stt.capabilities.streaming:
        frame = rtc.AudioFrame(data=pcm.tobytes(), sample_rate=rate, num_channels=1, samples_per_channel=len(pcm))
        return (await stt.recognize([frame])).alternatives[0].text
    stream, said = stt.stream(), []
    step = rate // 50
    for i in range(0, len(pcm), step):
        chunk = pcm[i:i + step]
        stream.push_frame(rtc.AudioFrame(data=chunk.tobytes(), sample_rate=rate, num_channels=1,
                                         samples_per_channel=len(chunk)))
        await asyncio.sleep(0.02)
    silence = np.zeros(step, "<i2")
    for _ in range(100):  # two seconds of quiet: the provider ends the utterance
        stream.push_frame(rtc.AudioFrame(data=silence.tobytes(), sample_rate=rate, num_channels=1,
                                         samples_per_channel=step))
        await asyncio.sleep(0.02)
    stream.flush()
    stream.end_input()

    async def read():
        async for ev in stream:
            if ev.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives:
                said.append(ev.alternatives[0].text)

    try:
        await asyncio.wait_for(read(), 10)
    except asyncio.TimeoutError:
        pass
    await stream.aclose()
    return " ".join(said)


async def main(language: str) -> None:
    targets = [(pid, os.environ[env], "") for pid, env in ENV.items() if os.environ.get(env)]
    if url := os.environ.get("OPENAI_COMPAT_URL"):
        targets.append(("openai_compat", os.environ.get("OPENAI_COMPAT_KEY", ""), url))
    if not targets:
        sys.exit("no provider key in the environment: " + ", ".join(ENV.values()) + ", OPENAI_COMPAT_URL")
    async with httpx.AsyncClient(timeout=20) as client, http_context.open():
        for pid, key, url in targets:
            p, conn = PROVIDERS[pid], Conn(pid, key, url)
            status = await check(conn, client)
            print(f"\n== {p.label}: key {'ok' if status.ok else status.code + ' ' + status.message}")
            if not status.ok:
                continue
            _, voices = await list_voices(conn, client, language=language, library=True)
            print("   voices:", ", ".join(f"{v.name} ({v.id[:12]})" for v in voices[:3]) or "(typed in)")
            voice = voices[0].id if voices else os.environ.get("OPENAI_COMPAT_VOICE", "")
            model = os.environ.get("OPENAI_COMPAT_TTS_MODEL", "") if pid == "openai_compat" else ""
            slot = Slot(provider=pid, voice=voice, model=model, base_url=url)
            try:
                first, pcm, rate = await speak(slot, key, language)
            except Exception as e:
                print(f"   speak: FAILED {type(e).__name__}: {e}")
                continue
            level = 20 * np.log10(np.sqrt(np.mean((pcm.astype(np.float32) / 32768) ** 2)) + 1e-9)
            print(f"   speak: first audio {first:.2f} s, {len(pcm) / rate:.1f} s at {rate} Hz, level {level:.1f} dBFS")
            if p.stt:
                model = os.environ.get("OPENAI_COMPAT_STT_MODEL", "") if pid == "openai_compat" else ""
                try:
                    print("   hear:", repr(await hear(Slot(provider=pid, model=model, base_url=url), key, language,
                                                   pcm, rate)))
                except Exception as e:
                    print(f"   hear: FAILED {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "en"))
