"""The LiveKit side of one call: turn-taking, Romanian speech, echo and goodbye handling.

The agent's thinking happens in its surogates session (SurogatesLLM); this class only decides
what to say aloud and when the call is over.
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from livekit import rtc
from livekit.agents import Agent, ModelSettings, StopResponse, llm, stt

from surogates.voice.text import caller_says_goodbye, is_echo, is_farewell, say_as, spoken_sentences

GREETING_DEFAULT = "Bună ziua! Cu ce vă pot ajuta?"
STILL_THERE = "Mai sunteți acolo?"
GOODBYE = "Vă mulțumesc că ați sunat. O zi bună!"
SORRY = "Îmi pare rău, am o problemă tehnică. Vă rog să sunați puțin mai târziu."
UNAVAILABLE = "Acest număr nu este disponibil momentan."
SENTENCE_PAUSE = 0.25  # Amami ends a sentence with almost no silence: without a breath, sentences run together
ECHO_WINDOW = 20.0  # seconds: what we said this recently can come back through a speakerphone

# Romanian is not in LiveKit's turn model: our STT's 640 ms pause ends the turn.
TURN_HANDLING = {
    "turn_detection": "stt",
    "endpointing": {"min_delay": 0.05, "max_delay": 1.5},
    "interruption": {"mode": "vad", "min_duration": 0.6, "min_words": 2,
                     "resume_false_interruption": True, "false_interruption_timeout": 1.5},
    "preemptive_generation": {"enabled": False},  # the agent runs tools: no speculative turns
}


@dataclass(frozen=True)
class CallConfig:
    greeting: str = GREETING_DEFAULT
    voice: str = "female"
    pronunciations: Mapping[str, str] = field(default_factory=dict)
    remember_callers: bool = False
    max_call_seconds: float = 600.0
    idle_ask_seconds: float = 30.0
    idle_hangup_seconds: float = 15.0

    @classmethod
    def from_routing(cls, cfg: Any) -> CallConfig:
        """Settings from the ops routing row. A bad value falls back to its default; the call never fails on it."""
        cfg, d = (cfg if isinstance(cfg, dict) else {}), cls()

        def text(key: str) -> str:
            v = cfg.get(key)
            return v.strip() if isinstance(v, str) and v.strip() else getattr(d, key)

        def seconds(key: str, lo: float, hi: float) -> float:
            v = cfg.get(key)
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi
            return float(v) if ok else getattr(d, key)

        pron = cfg.get("pronunciations")
        pron = {str(k): str(v) for k, v in pron.items() if str(k).strip() and str(v).strip()} if isinstance(pron, dict) else {}
        return cls(greeting=text("greeting"), voice=cfg.get("voice") if cfg.get("voice") in ("female", "male") else d.voice,
                   pronunciations=pron, remember_callers=cfg.get("remember_callers") is True,
                   max_call_seconds=seconds("max_call_seconds", 30, 3600),
                   idle_ask_seconds=seconds("idle_ask_seconds", 5, 300),
                   idle_hangup_seconds=seconds("idle_hangup_seconds", 5, 300))


def silence(rate: int, seconds: float) -> rtc.AudioFrame:
    n = int(rate * seconds)
    return rtc.AudioFrame(data=b"\0\0" * n, sample_rate=rate, num_channels=1, samples_per_channel=n)


class VoiceAgent(Agent):
    def __init__(self, config: CallConfig) -> None:
        super().__init__(instructions="")  # the agent's real prompt lives in its surogates session
        self.config = config
        self.recent: list[tuple[float, str]] = []  # (when, sentence) we spoke, for the echo check
        self.hangup_after_reply = False

    def _recent_said(self) -> list[str]:
        now = time.monotonic()
        self.recent = [(t, s) for t, s in self.recent if now - t < ECHO_WINDOW]
        return [s for _, s in self.recent]

    async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame],
                       model_settings: ModelSettings) -> AsyncIterable[stt.SpeechEvent]:
        """The caller's words, minus our own voice coming back through a speakerphone.

        Filtered here, before turn-taking sees it: LiveKit interrupts the agent on the words it hears
        while the agent speaks, so an echo dropped any later would already have cut the agent off.
        """
        async for ev in Agent.default.stt_node(self, audio, model_settings):
            text = ev.alternatives[0].text if isinstance(ev, stt.SpeechEvent) and ev.alternatives else ""
            if text and is_echo(text, self._recent_said(), while_speaking=self.session.agent_state == "speaking"):
                continue
            yield ev

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        text = new_message.text_content or ""
        if is_echo(text, self._recent_said()):
            raise StopResponse()  # our own voice through a speakerphone, not the caller
        if caller_says_goodbye(text):
            self.hangup_after_reply = True

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings) -> AsyncIterable[rtc.AudioFrame]:
        tts, said = self.session.tts, []
        async for sentence in spoken_sentences(text):
            if said:
                yield silence(tts.sample_rate, SENTENCE_PAUSE)
            said.append(sentence)
            self.recent.append((time.monotonic(), sentence))
            async for audio in tts.synthesize(say_as(sentence, self.config.pronunciations)):
                yield audio.frame
        if said and is_farewell(" ".join(said)):
            self.hangup_after_reply = True
