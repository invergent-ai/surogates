"""The LiveKit side of one call: turn-taking, speech, echo and goodbye handling.

The agent's thinking happens in its surogates session (SurogatesLLM); this class only decides
what to say aloud and when the call is over.
"""
from __future__ import annotations

import re
import time
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from livekit import rtc
from livekit.agents import Agent, ModelSettings, StopResponse, llm, stt

from surogates.voice.lines import Lines, default_lines, lines_from_routing
from surogates.voice.soundscape import SoundSettings
from surogates.voice.speech import Slot
from surogates.voice.text import caller_says_goodbye, is_echo, is_farewell, say_as, spoken_sentences

LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")  # a BCP 47 tag as Studio stores it: "ro", "en", "pt-BR"
SENTENCE_PAUSE = 0.25  # Amami ends a sentence with almost no silence: without a breath, sentences run together
ECHO_WINDOW = 20.0  # seconds: what we said this recently can come back through a speakerphone


@dataclass(frozen=True)
class CallConfig:
    language: str = "ro"
    lines: Lines = default_lines("ro")
    hearing: Slot = Slot()
    speaking: Slot = Slot()
    pronunciations: Mapping[str, str] = field(default_factory=dict)
    remember_callers: bool = False
    max_call_seconds: float = 600.0
    idle_ask_seconds: float = 30.0
    idle_hangup_seconds: float = 15.0
    sound: SoundSettings = field(default_factory=SoundSettings)

    @classmethod
    def from_routing(cls, cfg: Any) -> CallConfig:
        """Settings from the ops routing row. A bad value falls back to its default; the call never fails on it."""
        cfg, d = (cfg if isinstance(cfg, dict) else {}), cls()

        def seconds(key: str, lo: float, hi: float) -> float:
            v = cfg.get(key)
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi
            return float(v) if ok else getattr(d, key)

        pron = cfg.get("pronunciations")
        pron = ({k: v.strip() for k, v in pron.items() if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}
                if isinstance(pron, dict) else {})  # a non-text value would be spoken as "None" or "5"
        language = (cfg["language"].split("-")[0]  # "pt-BR" speaks pt: plugins and text rules take plain codes
                    if isinstance(cfg.get("language"), str) and LANGUAGE.match(cfg["language"]) else d.language)
        return cls(language=language, lines=lines_from_routing(language, cfg),
                   hearing=Slot.from_routing(cfg.get("hearing")),
                   speaking=Slot.from_routing(cfg.get("speaking"), voice=cfg.get("voice")),  # "voice": before providers
                   pronunciations=pron, remember_callers=cfg.get("remember_callers") is True,
                   max_call_seconds=seconds("max_call_seconds", 30, 3600),
                   idle_ask_seconds=seconds("idle_ask_seconds", 5, 300),
                   idle_hangup_seconds=seconds("idle_hangup_seconds", 5, 300),
                   sound=SoundSettings.from_routing(cfg.get("sound")))


def silence(rate: int, seconds: float) -> rtc.AudioFrame:
    n = int(rate * seconds)
    return rtc.AudioFrame(data=b"\0\0" * n, sample_rate=rate, num_channels=1, samples_per_channel=n)


class VoiceAgent(Agent):
    def __init__(self, config: CallConfig) -> None:
        super().__init__(instructions="")  # the agent's real prompt lives in its surogates session
        self.config = config
        self.recent: list[tuple[float, str]] = []  # (when, sentence) we spoke, for the echo check
        self.hangup_after_reply = False
        self.last_said = ""  # the last sentence spoken: did it ask for something to write down?

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
        if caller_says_goodbye(text, self.config.language):
            self.hangup_after_reply = True

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings) -> AsyncIterable[rtc.AudioFrame]:
        tts, said = self.session.tts, []
        async for sentence in spoken_sentences(text, self.config.language):
            if said:
                yield silence(tts.sample_rate, SENTENCE_PAUSE)
            said.append(sentence)
            self.recent.append((time.monotonic(), sentence))
            self.last_said = sentence
            # async with: a barge-in closes the generator here, and the sentence's TTS request with it
            async with tts.synthesize(say_as(sentence, self.config.pronunciations, self.config.language)) as stream:
                async for audio in stream:
                    yield audio.frame
        if said and is_farewell(" ".join(said), self.config.language):
            self.hangup_after_reply = True
