# Voice worker (`surogates voice`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `surogates voice` process that answers a LiveKit SIP call with the agent its number belongs to, in Romanian, end to end on the dev laptop.

**Architecture:** LiveKit Agents runs one job per call. Three small plugins adapt our services to it: `RoSTT` (our streaming STT WebSocket), `RoTTS` (our `/v1/audio/speech`), and `SurogatesLLM`, which drives the call's agent session through the runtime's internals (`SessionStore`, the shared work queue, Redis pub/sub) instead of HTTP. A Romanian text layer and the phone EQ from the Twilio prototype shape what is spoken.

**Tech Stack:** Python 3.12, `livekit-agents[silero]~=1.8` (1.8.4), `livekit-api`, numpy/scipy/soxr, `websockets`, `httpx`, SQLAlchemy async, Redis.

**Spec:** `docs/superpowers/specs/2026-10-05-voice-channel-design.md`

## Global Constraints

- Python `>=3.12`; LiveKit Agents `~=1.8` only as the optional extra `voice` — the core install must not import `livekit`.
- Nothing under `surogates/voice/` is imported by the API, worker or channels processes.
- Speech services in dev: STT `ws://127.0.0.1:18001/v1/audio/streams`, TTS `http://127.0.0.1:18080/v1/audio/speech` (SSH tunnel to prod ns `voice`).
- STT protocol: first message `{"type":"ready"}`; send binary 16 kHz s16le mono; text `{"type":"finish"}` ends; events `partial` / `final` (`reason`: pause | max_duration | idle | eof, `idle` may carry empty text) / `done` / `error`.
- TTS request body is exactly `{"input","voice","response_format":"pcm","stream_format":"audio"}` — any other field is a 400. Rate comes from the `X-Audio-Sample-Rate` header (24000 for amami).
- Voices: `female`, `male`.
- Turn handling: `turn_detection="stt"`, `min_delay` 0.05, `max_delay` 1.5, interruption `vad` 0.6 s / 2 words with false-interruption resume (1.5 s), **preemptive generation off**.
- A call session: `channel="voice"`, one session per call (`session_key="agent:voice:call:<call_id>"`), memory boundary `voice:call:<call_id>` unless the agent's `remember_callers` is true **and** the caller number is known, then `phone:<digits>`.
- Caller numbers are stored without the leading `+` (`identity.py:166-168`). A withheld number is the shadow identity `anonymous`.
- `voice` is in `END_USER_CHANNELS` and `MANAGED_CHANNELS`; it is **not** in `ADAPTER_CHANNELS`, `INTERACTIVE_PROMPT_CHANNELS`, `DIRECT_UI_CHANNELS`, `INBOX_NOTIFY_CHANNELS`.
- Commits are authored by Madalin, never mention an AI assistant, and are written with `git commit -F -` and a quoted heredoc. Branch: `feat/voice-channel`.
- Tests are functional only (`tests/README.md`): no default-value or inventory tests. Run with `.venv/bin/python -m pytest <file> -q` (plain `uv run` re-syncs the env).

## Review Focus

- **Withheld caller ID** (`sip.phoneNumber` empty or `anonymous`): the call is answered, the session uses the shared `anonymous` identity and a per-call memory boundary even when `remember_callers` is on — Task 6, `test_open_call_withheld_number_never_remembers`.
- **A number nobody owns** (no routing row): the caller hears "Acest număr nu este disponibil" and the call ends; no session is created — Task 9, `test_call_info_*` plus the manual e2e check.
- **A routing config with wrong types or junk** (ops bug, hand-edited row): the call uses defaults, never crashes — Task 8, `test_call_config_survives_bad_values`.
- **Interrupted after the whole answer was already written vs. mid-generation:** history stays true in both cases (synthetic reply vs. a note on the next message) — Task 6, two `record_heard` tests.
- **STT idle finals with empty text and STT error frames:** no ghost turns; an error surfaces instead of hanging — Task 3, `test_translate_*` and `test_stream_raises_on_error_frame`.

---

## File Structure

| File | Responsibility |
|---|---|
| `surogates/voice/__init__.py` | Package marker, docstring only. |
| `surogates/voice/text.py` | Romanian text layer: `say_as`, `clean`, `SentenceSplitter`, `spoken_sentences`, `is_echo`, `caller_says_goodbye`, `is_farewell`. Pure, stdlib only. |
| `surogates/voice/audio.py` | `Trim` and `PhoneVoice`: silence trimming, EQ, gain, limiter on TTS PCM. |
| `surogates/voice/stt.py` | `RoSTT` / `RoSTTStream`, `translate()` (server event → LiveKit `SpeechEvent`s). |
| `surogates/voice/tts.py` | `RoTTS` / `RoChunkedStream`. |
| `surogates/voice/sessions.py` | `CallTarget`, `VoiceSessions.open_call`, `CallSession.send/stream/interrupt/record_heard`, `question_text`. |
| `surogates/voice/llm.py` | `SurogatesLLM` / `SurogatesStream`, `latest_user_text`. |
| `surogates/voice/agent.py` | `CallConfig`, fixed phrases, `TURN_HANDLING`, `VoiceAgent` (echo, goodbye, `tts_node`). |
| `surogates/voice/worker.py` | `call_info`, `Runtime`, `entrypoint`, `run_voice`. |
| `surogates/harness/prompts/platforms/voice.md` | Platform hint for spoken answers. |
| `surogates/channels/constants.py`, `surogates/channels/memory_boundary.py` | Register `voice`. |
| `surogates/config.py` | `VoiceSettings`, `Settings.voice`. |
| `surogates/cli/main.py` | `surogates voice`. |
| `pyproject.toml` | Optional extra `voice`. |
| `scripts/voice-dev/` | Local LiveKit + SIP, trunk and dispatch, routing row, README. |

---

### Task 1: Romanian text layer

**Files:**
- Create: `surogates/voice/__init__.py`
- Create: `surogates/voice/text.py`
- Test: `tests/test_voice_text.py`

**Interfaces:**
- Produces:
  - `say_as(text: str, table: Mapping[str, str]) -> str`
  - `clean(text: str) -> str`
  - `class SentenceSplitter: push(delta: str) -> list[str]; flush() -> str`
  - `async def spoken_sentences(deltas: AsyncIterable[str]) -> AsyncIterator[str]` (split + clean + repeated-preamble filter)
  - `is_echo(heard: str, recent: list[str], *, while_speaking: bool = False) -> bool`
  - `caller_says_goodbye(text: str) -> bool`, `is_farewell(reply: str) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice_text.py
"""What the phone voice says and hears: ported from the Twilio prototype, then fixed for Romanian abbreviations."""
from surogates.voice.text import (
    SentenceSplitter, caller_says_goodbye, clean, is_echo, is_farewell, say_as, spoken_sentences,
)


def test_say_as_spells_brand_and_agent_names():
    table = {"Nvidia": "Envidia", "DAX": "Dax"}
    assert say_as("Surogate spune că Nvidia și DAX cresc.", table) == "Surogheit spune că Envidia și Dax cresc."
    assert say_as("Avionul A220-300 aterizează.", {}) == "Avionul A 220 aterizează."
    assert say_as("Nvidiaa nu e în tabel.", table) == "Nvidiaa nu e în tabel."


def test_clean_drops_markdown_and_domains():
    assert clean("**Sursa** — goldring.ro") == "Sursa, goldring"


def test_splitter_streams_sentences_and_keeps_the_tail():
    s = SentenceSplitter()
    assert s.push("Bună ziua. Cu ce") == ["Bună ziua."]
    assert s.push(" vă pot ajuta?") == []
    assert s.flush() == "Cu ce vă pot ajuta?"


def test_splitter_does_not_cut_after_abbreviations_or_initials():
    s = SentenceSplitter()
    out = s.push("Locuiesc pe str. Mihai Eminescu nr. 5. Firma X S.A. are profit. Gata")
    assert out == ["Locuiesc pe str. Mihai Eminescu nr. 5.", "Firma X S.A. are profit."]
    assert s.flush() == "Gata"


def test_splitter_cuts_a_glued_preamble_and_answer():
    assert SentenceSplitter().push("Verific acum.Azi cursul e 4,97. ") == ["Verific acum.", "Azi cursul e 4,97."]


async def test_spoken_sentences_drops_a_repeated_preamble():
    async def deltas():
        for d in ("O clipă, verific cursul. ", "Imediat, verific din nou. ", "Euro e 4,97 lei."):
            yield d
    assert [s async for s in spoken_sentences(deltas())] == ["O clipă, verific cursul.", "Euro e 4,97 lei."]


def test_echo_of_our_own_sentence_is_recognised():
    said = ["Te pot ajuta cu întrebări legate de știrile zilei."]
    assert is_echo("întrebări legate de știrile zilei", said)
    assert not is_echo("care sunt știrile de azi", said)
    assert not is_echo("da", said)


def test_goodbye_and_farewell():
    assert caller_says_goodbye("Mulțumesc, atât.")
    assert caller_says_goodbye("Bine, pa")
    assert not caller_says_goodbye("Papa Francisc a spus ceva")
    assert is_farewell("La revedere, o zi bună!")
    assert not is_farewell("La revedere? Mai aveți o întrebare?")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_text.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.voice'`

- [ ] **Step 3: Implement**

```python
# surogates/voice/__init__.py
"""Phone calls: LiveKit SIP in, the agent's session in the middle, Romanian speech out (``surogates voice``)."""
```

```python
# surogates/voice/text.py
"""Romanian text for the phone voice: what the TTS should say, and what the caller just said.

Ported from an earlier Twilio prototype. It shapes speech
only: the session keeps the agent's text exactly as written.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import AsyncIterable, AsyncIterator, Mapping

BRAND = re.compile(r"\bsurogate\b", re.I)
GLUED_NUMBER = re.compile(r"\b([A-Za-z]+)(\d{2,})\b")  # "A220" is a name and a number, not a code to dictate
MODEL_VARIANT = re.compile(r"\b([A-Za-z]+\d{2,})-\d{2,}\b")  # "A220-300": Amami dictates the dash form
HAS_WORDS = re.compile(r"\w")


def say_as(text: str, table: Mapping[str, str]) -> str:
    """Spell names the way a Romanian caller says them. Table keys match whole words, case-sensitively."""
    text = BRAND.sub("Surogheit", text)  # hard g; "Surogeit" is wrong
    if table:
        keys = "|".join(map(re.escape, sorted(table, key=len, reverse=True)))
        text = re.sub(rf"(?<![\w&])({keys})(?![\w&])", lambda m: table[m.group(1)], text)
    return GLUED_NUMBER.sub(r"\1 \2", MODEL_VARIANT.sub(r"\1", text))


def clean(text: str) -> str:
    text = re.sub(r"\s*[—–]\s*", ", ", text)  # a dash is where a speaker pauses
    text = re.sub(r"\b([\w-]+)\.(ro|com|net|org|eu)\b", r"\1", text)  # sources are said by name
    return re.sub(r"[*#_`>]", "", text).strip()


# A sentence ends at punctuation + space, or where a preamble and the answer arrive glued ("acum.Azi").
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[a-zăâîșț0-9]{2}[.!?…])(?=[A-ZĂÂÎȘȚ])")
# ...but not after "nr.", "str.", "Dl." or an initial ("S.A.", "M. Eminescu").
ABBREVIATION_END = re.compile(  # the initial is capital-only: "Prețul e." still ends a sentence
    r"((?i:\b(?:nr|dl|dna|dra|str|tel|prof|dr|ing|art|alin|pct|etc|ex|sf|bd|jud|mun))|\b[A-ZĂÂÎȘȚ])\.$")


class SentenceSplitter:
    """Cuts the agent's streamed text into sentences, as soon as each one is complete."""

    def __init__(self) -> None:
        self._buf = ""

    def push(self, delta: str) -> list[str]:
        *done, self._buf = SENTENCE_END.split(self._buf + delta)
        out, carry = [], ""
        for piece in done:
            piece = f"{carry} {piece}" if carry else piece
            if ABBREVIATION_END.search(piece):
                carry = piece
                continue
            carry = ""
            if HAS_WORDS.search(sentence := clean(piece)):
                out.append(sentence)
        if carry:
            self._buf = f"{carry} {self._buf}"
        return out

    def flush(self) -> str:
        tail, self._buf = clean(self._buf), ""
        return tail if HAS_WORDS.search(tail) else ""


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


# The agent's "I'm on it" line. Once per answer is natural; the model sometimes says it again after the
# tool returns ("O clipă, mă uit…" then "Imediat, verific…"), and twice sounds broken.
PREAMBLE = re.compile(r"^(o clipă|imediat|stai puțin|un moment|o secundă|mă uit|verific|caut|acum verific)\b.{0,80}"
                      r"(verific|mă uit|caut|văd|iau)", re.I)


def is_preamble(sentence: str) -> bool:
    return bool(PREAMBLE.search(sentence)) and len(words(sentence)) <= 14


async def spoken_sentences(deltas: AsyncIterable[str]) -> AsyncIterator[str]:
    """One answer's sentences in speaking order, without a second preamble."""
    splitter, preambled = SentenceSplitter(), False

    def keep(sentence: str) -> bool:
        nonlocal preambled
        if not is_preamble(sentence):
            return True
        first, preambled = not preambled, True
        return first

    async for delta in deltas:
        for sentence in splitter.push(delta):
            if keep(sentence):
                yield sentence
    if (tail := splitter.flush()) and keep(tail):
        yield tail


def is_echo(heard: str, recent: list[str], *, while_speaking: bool = False) -> bool:
    """Is this "caller" text our own voice coming back (speakerphone)? STT garbles the echo, so compare words."""
    h = words(heard)
    if while_speaking and len(h) >= 2:
        ours = {w for s in recent for w in words(s)}
        content = [w for w in h if len(w) >= 3]
        loose = lambda w: w in ours or any(o.startswith(w) or w.startswith(o) for o in ours if len(o) >= 4)  # noqa: E731
        if content and sum(map(loose, content)) >= 0.6 * len(content):
            return True
    if len(h) < 3:
        return False
    for s in recent:
        m = difflib.SequenceMatcher(None, h, words(s), autojunk=False)
        # echo keeps runs of our words; a caller reusing a few of them does not
        if sum(b.size for b in m.get_matching_blocks() if b.size >= 2) >= 0.45 * len(h):
            return True
    return False


BYE = re.compile(r"\b(la revedere|o zi bună|mulțumesc,? atât|asta e tot|gata,? mulțumesc|nimic altceva)\b"
                 r"|\b(pa[ -]?pa|pa|mersi|ciao|bye)[.!]?$", re.I)  # "pa" only as the last word: "Papa Francisc…"
FAREWELL = re.compile(r"^\W*(pa|la revedere|o zi bună|cu plăcere|mulțumesc|spor|numai bine|toate cele bune)\b"
                      r"[^?]{0,60}$", re.I)


def caller_says_goodbye(text: str) -> bool:
    return bool(BYE.search(text.strip()))


def is_farewell(reply: str) -> bool:
    """A whole reply that is only a goodbye: the call can end after it."""
    return bool(FAREWELL.match(reply.strip()))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_voice_text.py -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add surogates/voice/__init__.py surogates/voice/text.py tests/test_voice_text.py
git commit -F - <<'EOF'
feat(voice): say Romanian the way a caller hears it

Port the phone prototype's text layer (pronunciations, cleaning, sentence
splitting, repeated preamble, echo, goodbye) and stop splitting after
"nr.", "str." and initials.
EOF
```

---

### Task 2: Phone audio and the `voice` extra

**Files:**
- Modify: `pyproject.toml` (add `[project.optional-dependencies]` `voice`)
- Create: `surogates/voice/audio.py`
- Test: `tests/test_voice_audio.py`

**Interfaces:**
- Produces: `class Trim(rate: int, ...)`, `class PhoneVoice(rate: int, gain_db=10.0, highpass_hz=200.0, presence_db=4.0, presence_hz=2500.0)` with `__call__(pcm: bytes, last: bool = False) -> bytes` (s16le mono in and out, same rate).

- [ ] **Step 1: Add the extra and install it**

In `pyproject.toml`, after the `dependencies = [...]` block of `[project]` (if a `[project.optional-dependencies]` table already exists, add the key to it):

```toml
[project.optional-dependencies]
voice = [
    "livekit-agents[silero]~=1.8",
    "livekit-api~=1.2",
    "numpy>=2.0",
    "scipy>=1.14",
    "soxr>=1.0",
    "websockets>=13",
]
```

Run: `uv lock && VIRTUAL_ENV=$PWD/.venv uv pip install -e '.[voice]'`
Expected: `livekit-agents==1.8.4` installed; `git diff --stat` shows `pyproject.toml` and `uv.lock` only.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_voice_audio.py
"""TTS speech made fit for a phone line: no padding silence, telephone level, never clipped."""
import audioop

import numpy as np

from surogates.voice.audio import PhoneVoice

RATE = 24000


def _pcm(x: np.ndarray) -> bytes:
    return (x * 32767).astype("<i2").tobytes()


def test_padding_silence_is_cut_and_the_speech_kept():
    voiced = (np.sin(np.arange(RATE) * 2 * np.pi * 300 / RATE) * 0.3).astype(np.float32)
    padded = np.concatenate([np.zeros(int(0.45 * RATE), np.float32), voiced, np.zeros(int(0.33 * RATE), np.float32)])
    voice, out = PhoneVoice(RATE), b""
    for i in range(0, len(padded), 2400):  # streamed in 100 ms chunks, like the TTS
        out += voice(_pcm(padded[i:i + 2400]))
    out += voice(b"", last=True)
    assert 1.0 <= len(out) / 2 / RATE <= 1.15


def test_louder_but_never_clipped():
    quiet = (np.sin(np.arange(RATE) * 2 * np.pi * 440 / RATE) * 0.05).astype(np.float32)
    out = PhoneVoice(RATE)(_pcm(quiet), last=True)
    assert audioop.rms(out, 2) > 2.5 * audioop.rms(_pcm(quiet), 2)
    loud = (np.sin(np.arange(RATE) * 2 * np.pi * 1000 / RATE) * 0.95).astype(np.float32)
    peak = np.abs(np.frombuffer(PhoneVoice(RATE)(_pcm(loud), last=True), "<i2"))
    assert (peak >= 32700).mean() < 0.01


def test_all_silence_yields_nothing():
    assert PhoneVoice(RATE)(b"\0\0" * RATE, last=True) == b""
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_audio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.voice.audio'`

- [ ] **Step 4: Implement**

```python
# surogates/voice/audio.py
"""The agent's voice made fit for a phone line, streamed chunk by chunk (filter state carries over).

Amami pads every sentence with ~0.45 s of silence before and ~0.33 s after, and speaks at about
-28 dBFS; telephone speech sits near -16..-18 dBFS and quiet audio sounds distant after G.711.
Ported from the Twilio prototype's ``phone_audio.py``; LiveKit SIP now does the 8 kHz encoding.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi


def _peaking(f0: float, gain_db: float, q: float, fs: float) -> np.ndarray:
    """RBJ peaking EQ as one second-order section."""
    a, w = 10 ** (gain_db / 40), 2 * np.pi * f0 / fs
    alpha = np.sin(w) / (2 * q)
    b = [1 + alpha * a, -2 * np.cos(w), 1 - alpha * a]
    d = [1 + alpha / a, -2 * np.cos(w), 1 - alpha / a]
    return np.array([[*(x / d[0] for x in b), 1.0, d[1] / d[0], d[2] / d[0]]])


class Trim:
    """Cut the silence around a sentence, keep a hair of it. Holds back ``hold`` seconds, which may be tail silence."""

    def __init__(self, rate: int, threshold=0.01, keep_lead=0.05, keep_tail=0.08, hold=0.5):
        self.threshold = threshold
        self.keep_lead, self.keep_tail, self.hold = int(keep_lead * rate), int(keep_tail * rate), int(hold * rate)
        self.started, self.pending = False, np.zeros(0, np.float32)

    def __call__(self, x: np.ndarray, last: bool = False) -> np.ndarray:
        x = np.concatenate([self.pending, x])
        if not self.started:
            loud = np.flatnonzero(np.abs(x) > self.threshold)
            if not len(loud):
                self.pending = x[-self.keep_lead:] if not last else np.zeros(0, np.float32)
                return np.zeros(0, np.float32)
            self.started, x = True, x[max(0, loud[0] - self.keep_lead):]
        if last:
            loud = np.flatnonzero(np.abs(x) > self.threshold)
            self.pending = np.zeros(0, np.float32)
            return x[: loud[-1] + self.keep_tail] if len(loud) else np.zeros(0, np.float32)
        out, self.pending = x[: max(0, len(x) - self.hold)], x[max(0, len(x) - self.hold):]
        return out


class PhoneVoice:
    """TTS speech (s16le mono) -> the same rate: trimmed, 200 Hz high-pass, presence lift, louder, soft-limited."""

    def __init__(self, rate: int, gain_db=10.0, highpass_hz=200.0, presence_db=4.0, presence_hz=2500.0):
        # below ~200 Hz a handset only turns voice into boom; 2-3 kHz is where consonants live
        sos = np.vstack([butter(2, highpass_hz, "highpass", fs=rate, output="sos"),
                         _peaking(presence_hz, presence_db, 0.9, rate)])
        self.sos, self.zi = sos, sosfilt_zi(sos) * 0.0
        self.gain = 10 ** (gain_db / 20)
        self.trim = Trim(rate)
        self.rest = b""

    def __call__(self, pcm: bytes, last: bool = False) -> bytes:
        pcm, self.rest = self.rest + pcm, b""
        if len(pcm) % 2:
            pcm, self.rest = pcm[:-1], pcm[-1:]
        x = self.trim(np.frombuffer(pcm, "<i2").astype(np.float32) / 32768, last=last)
        if not len(x):
            return b""
        y, self.zi = sosfilt(self.sos, x, zi=self.zi)
        y = 0.97 * np.tanh(y * self.gain / 0.97)  # soft limiter: loud, never clipped
        return (y * 32767).astype("<i2").tobytes()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_voice_audio.py -q`
Expected: 3 passed. (`audioop` is still in 3.12 with a DeprecationWarning; the test only uses it for RMS.)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock surogates/voice/audio.py tests/test_voice_audio.py
git commit -F - <<'EOF'
feat(voice): trim and shape the agent's voice for a phone line

Adds the optional `voice` extra (LiveKit Agents 1.8, numpy, scipy, soxr,
websockets). Amami's ~0.45 s lead silence is cut, which is speech the caller
hears that much sooner.
EOF
```

---

### Task 3: RoSTT — our streaming STT as a LiveKit plugin

**Files:**
- Create: `surogates/voice/stt.py`
- Test: `tests/test_voice_stt.py`

**Interfaces:**
- Consumes: STT protocol from Global Constraints.
- Produces: `class RoSTT(stt.STT)(*, url: str)`; `RoSTT.stream() -> RoSTTStream`; `translate(ev: dict, speaking: bool) -> tuple[list[stt.SpeechEvent], bool]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice_stt.py
"""Our Romanian STT's events become LiveKit's: final before end-of-speech, no ghost turns from idle finals."""
import json

import pytest
from livekit import rtc
from livekit.agents import APIError, stt
from websockets.asyncio.server import serve

from surogates.voice.stt import RoSTT, translate

T = stt.SpeechEventType


def _kinds(events):
    return [(e.type, e.alternatives[0].text if e.alternatives else "") for e in events]


def test_translate_a_turn():
    out, speaking = translate({"type": "partial", "text": "bună"}, False)
    assert _kinds(out) == [(T.START_OF_SPEECH, ""), (T.INTERIM_TRANSCRIPT, "bună")] and speaking
    out, speaking = translate({"type": "final", "text": "Bună ziua", "reason": "pause"}, True)
    assert _kinds(out) == [(T.FINAL_TRANSCRIPT, "Bună ziua"), (T.END_OF_SPEECH, "")] and not speaking


def test_translate_idle_final_is_not_a_turn():
    assert translate({"type": "final", "text": "", "reason": "idle"}, False) == ([], False)
    out, speaking = translate({"type": "final", "text": "  ", "reason": "idle"}, True)
    assert _kinds(out) == [(T.END_OF_SPEECH, "")] and not speaking


def _frame():
    return rtc.AudioFrame(data=b"\0\0" * 1600, sample_rate=16000, num_channels=1, samples_per_channel=1600)


async def test_stream_against_a_server():
    received = []

    async def handler(ws):
        await ws.send(json.dumps({"type": "ready", "sample_rate": 16000}))
        received.append(await ws.recv())
        for ev in ({"type": "partial", "text": "bună"}, {"type": "final", "text": "Bună ziua", "reason": "pause"}):
            await ws.send(json.dumps(ev))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stream = RoSTT(url=f"ws://127.0.0.1:{port}").stream()
        stream.push_frame(_frame())
        events = []
        async for ev in stream:
            events.append(ev)
            if ev.type == T.END_OF_SPEECH:
                break
        await stream.aclose()
    assert _kinds(events) == [(T.START_OF_SPEECH, ""), (T.INTERIM_TRANSCRIPT, "bună"),
                              (T.FINAL_TRANSCRIPT, "Bună ziua"), (T.END_OF_SPEECH, "")]
    assert isinstance(received[0], bytes) and len(received[0]) == 3200


async def test_stream_raises_on_error_frame():
    async def handler(ws):
        await ws.send(json.dumps({"type": "ready"}))
        await ws.send(json.dumps({"type": "error", "error": {"message": "model failed"}}))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        from livekit.agents import APIConnectOptions
        stream = RoSTT(url=f"ws://127.0.0.1:{port}").stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_frame(_frame())
        with pytest.raises(APIError):
            async for _ in stream:
                pass
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_stt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.voice.stt'`

- [ ] **Step 3: Implement**

```python
# surogates/voice/stt.py
"""Our Romanian streaming STT (``surogate serve --stt``, ``/v1/audio/streams``) as a LiveKit STT.

The server ends an utterance itself after 640 ms of quiet (Silero VAD) and re-decodes it, so the
session runs with ``turn_detection="stt"``: LiveKit commits the turn on END_OF_SPEECH, which we
send right after the FINAL (LiveKit ignores an end-of-speech that has no transcript yet).
"""
from __future__ import annotations

import asyncio
import json

from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectionError, APIConnectOptions, NotGivenOr, stt,
)
from livekit.agents.utils import AudioBuffer
from websockets.asyncio.client import connect

T = stt.SpeechEventType


def _event(kind: stt.SpeechEventType, text: str = "") -> stt.SpeechEvent:
    alternatives = [stt.SpeechData(language="ro", text=text)] if text else []
    return stt.SpeechEvent(type=kind, alternatives=alternatives)


def translate(ev: dict, speaking: bool) -> tuple[list[stt.SpeechEvent], bool]:
    """One server event -> LiveKit events, and whether the caller is now mid-utterance."""
    kind, text = ev.get("type"), (ev.get("text") or "").strip()
    if kind == "partial" and text:
        return ([] if speaking else [_event(T.START_OF_SPEECH)]) + [_event(T.INTERIM_TRANSCRIPT, text)], True
    if kind == "final":
        if text:  # FINAL before END_OF_SPEECH: in "stt" turn detection the end commits the turn
            out = [] if speaking else [_event(T.START_OF_SPEECH)]
            return out + [_event(T.FINAL_TRANSCRIPT, text), _event(T.END_OF_SPEECH)], False
        return ([_event(T.END_OF_SPEECH)] if speaking else []), False  # "idle": 10 s without speech
    return [], speaking


class RoSTT(stt.STT):
    def __init__(self, *, url: str) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=True,
                                                          offline_recognize=False))
        self._url = url

    @property
    def model(self) -> str:
        return "jackrabbit-110m-ro-streaming"

    @property
    def provider(self) -> str:
        return "surogate"

    async def _recognize_impl(self, buffer: AudioBuffer, *, language: NotGivenOr[str] = NOT_GIVEN,
                              conn_options: APIConnectOptions) -> stt.SpeechEvent:
        raise NotImplementedError("RoSTT only streams")

    def stream(self, *, language: NotGivenOr[str] = NOT_GIVEN,
               conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> RoSTTStream:
        return RoSTTStream(stt=self, url=self._url, conn_options=conn_options)


class RoSTTStream(stt.RecognizeStream):
    def __init__(self, *, stt: RoSTT, url: str, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=16000)  # LiveKit resamples to 16 kHz
        self._url = url

    async def _run(self) -> None:
        try:
            ws = await connect(self._url, max_size=None, open_timeout=5)  # 429 when every stream slot is taken
        except Exception as e:
            raise APIConnectionError(f"STT unavailable: {e!r}") from e
        async with ws:
            if json.loads(await ws.recv()).get("type") != "ready":
                raise APIConnectionError("STT did not say ready")

            async def send() -> None:
                async for item in self._input_ch:
                    if not isinstance(item, self._FlushSentinel):
                        await ws.send(item.data.tobytes())
                await ws.send(json.dumps({"type": "finish"}))

            sender = asyncio.create_task(send())
            speaking = False
            try:
                async for raw in ws:
                    ev = json.loads(raw)
                    if ev.get("type") == "error":
                        raise APIConnectionError(f"STT error: {(ev.get('error') or {}).get('message', '')}")
                    if ev.get("type") == "done":
                        break
                    out, speaking = translate(ev, speaking)
                    for e in out:
                        self._event_ch.send_nowait(e)
            finally:
                sender.cancel()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_voice_stt.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add surogates/voice/stt.py tests/test_voice_stt.py
git commit -F - <<'EOF'
feat(voice): hear the caller through our Romanian streaming STT

RoSTT turns the server's partial/final events into LiveKit speech events,
final before end-of-speech, and drops the empty "idle" finals.
EOF
```

---

### Task 4: RoTTS — our TTS as a LiveKit plugin

**Files:**
- Create: `surogates/voice/tts.py`
- Test: `tests/test_voice_tts.py`

**Interfaces:**
- Consumes: `PhoneVoice(rate)` (Task 2).
- Produces: `class RoTTS(tts.TTS)(*, url: str, voice: str = "female", sample_rate: int = 24000, client: httpx.AsyncClient | None = None)`; `RoTTS.synthesize(text) -> RoChunkedStream` yielding 24 kHz mono frames already shaped by `PhoneVoice`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice_tts.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_tts.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.voice.tts'`

- [ ] **Step 3: Implement**

```python
# surogates/voice/tts.py
"""Our Romanian TTS (``surogate serve --tts``, ``POST /v1/audio/speech``) as a LiveKit TTS.

One request per sentence (the agent's tts_node splits). The server rejects unknown fields, so the
body is exactly four keys; audio streams back as raw PCM at ``X-Audio-Sample-Rate``.
"""
from __future__ import annotations

import httpx
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_voice_tts.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add surogates/voice/tts.py tests/test_voice_tts.py
git commit -F - <<'EOF'
feat(voice): speak through our Romanian TTS

RoTTS sends the four fields the server accepts, refuses audio at an
unexpected rate, and passes LiveKit trimmed, phone-shaped PCM.
EOF
```

---

### Task 5: Register the `voice` channel

**Files:**
- Create: `surogates/harness/prompts/platforms/voice.md`
- Modify: `surogates/channels/constants.py` (`END_USER_CHANNELS`)
- Modify: `surogates/channels/memory_boundary.py:21` (`MANAGED_CHANNELS`)
- Test: `tests/test_voice_channel.py`

**Interfaces:**
- Produces: sessions with `channel="voice"` get the voice platform hint and an honoured `config["memory_boundary"]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice_channel.py
"""A voice session is spoken to, and its memory stays inside the boundary the call chose."""
from types import SimpleNamespace

from surogates.channels.memory_boundary import session_memory_boundary
from surogates.harness.prompt_library import default_library


def test_voice_sessions_get_a_spoken_platform_hint():
    hint = default_library().platform_hint("voice")
    assert hint and "telefon" in hint.lower() and "markdown" in hint.lower()


def test_a_voice_call_keeps_its_own_memory_boundary():
    call = SimpleNamespace(channel="voice", config={"memory_boundary": "voice:call:SCL_1"}, id="s1")
    assert session_memory_boundary(call) == "voice:call:SCL_1"
    remembered = SimpleNamespace(channel="voice", config={"memory_boundary": "phone:40722000111"}, id="s2")
    assert session_memory_boundary(remembered) == "phone:40722000111"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_channel.py -q`
Expected: FAIL — the hint is `None`; the boundary is `None` (voice is not a managed channel).

- [ ] **Step 3: Implement**

```markdown
<!-- surogates/harness/prompts/platforms/voice.md -->
---
name: voice
channel: voice
description: Platform hint for phone calls — every word is spoken aloud in Romanian, so answers are short and plain.
---
Ești la telefon: tot ce scrii este citit cu voce tare apelantului, în română. Răspunde scurt, în propoziții simple, de obicei una sau două, ca într-o convorbire. Nu folosi markdown, liste, tabele, emoji sau linkuri: nu se aud. Spune numerele și datele așa cum le-ar spune un om. Când trebuie să cauți ceva, spune mai întâi o propoziție scurtă, de exemplu „O clipă, verific.”, apoi caută. Pune o singură întrebare odată. Nu ceri apelantului să citească sau să scrie ceva. Numărul de telefon al apelantului nu dovedește cine este: nu dezvălui date personale pe baza lui. Când apelantul își ia la revedere, răspunde cu o singură propoziție de rămas-bun.
```

In `surogates/channels/constants.py`:

```python
END_USER_CHANNELS = frozenset(
    {"web", "website", "slack", "telegram", "teams", "whatsapp", "voice"}
)
```

In `surogates/channels/memory_boundary.py:21`:

```python
# Channel platforms whose sessions are memory-partitioned by conversation.
# ``voice`` always persists its boundary at session creation: one call, or one
# caller's number when the agent remembers callers.
MANAGED_CHANNELS: frozenset[str] = frozenset({"slack", "telegram", "whatsapp", "voice"})
```

- [ ] **Step 4: Run the tests, then the channel suites they touch**

Run: `.venv/bin/python -m pytest tests/test_voice_channel.py -q && .venv/bin/python -m pytest tests -q -k "memory_boundary or end_user or platform_hint"`
Expected: all pass. A failing inventory assertion that lists `MANAGED_CHANNELS` or `END_USER_CHANNELS` members must be updated to include `voice`, not worked around.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/prompts/platforms/voice.md surogates/channels/constants.py surogates/channels/memory_boundary.py tests/test_voice_channel.py
git commit -F - <<'EOF'
feat(voice): make "voice" a channel the harness knows

A spoken platform hint, end-user enrollment, and a memory boundary that is
always the call's own unless the agent chose to remember callers.
EOF
```

---

### Task 6: CallSession — the call's agent session through the runtime's internals

**Files:**
- Create: `surogates/voice/sessions.py`
- Test: `tests/test_voice_sessions.py`

**Interfaces:**
- Consumes: `get_or_create_channel_identity` (`channels/identity.py:120`), `get_or_create_channel_session` (`:314`), `build_principal_stamp` (`channels/inbound.py:370`), `enqueue_session` and `INTERRUPT_CHANNEL_PREFIX` (`config.py:203,229`), `try_resolve_text_answer` (`session/interactive_input.py:329`), `SessionStore.emit_event/get_events/get_session/resume_session`.
- Produces:
  - `CallTarget(org_id: UUID, agent_id: str, remember_callers: bool = False)`
  - `VoiceSessions(*, store, redis, session_factory, storage=None, settings=None).open_call(target, *, call_id: str, called: str, caller: str | None, greeting: str = "") -> CallSession`
  - `CallSession.send(text: str) -> int` (event id to stream after)
  - `CallSession.stream(after: int) -> AsyncIterator[str]` (the turn's text; ends at the final answer, a terminal event, or an `ask_user_question`)
  - `CallSession.interrupt() -> None`
  - `CallSession.record_heard(heard: str) -> None`
  - `normalize_caller(raw: str | None) -> str` (digits without `+`, or `"anonymous"`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice_sessions.py
"""A call drives its agent session directly: post, stream, stop, and keep the history true after a barge-in."""
import json
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.voice import sessions as voice_sessions
from surogates.voice.sessions import HEARD_NONE, CallSession, CallTarget, VoiceSessions, normalize_caller


class _PubSub:
    async def subscribe(self, *_): ...
    async def get_message(self, **_): return None
    async def aclose(self): ...


class _Redis:
    def __init__(self):
        self.queued, self.published = [], []
    async def zadd(self, key, mapping): self.queued.append(mapping)
    async def publish(self, channel, payload): self.published.append((channel, json.loads(payload)))
    def pubsub(self): return _PubSub()


@pytest.fixture
async def call(sf):
    store = SessionStore(sf, redis=None)
    org = uuid4()
    s = await store.create_session(user_id=None, org_id=org, agent_id="agent-1", channel="voice")
    return CallSession(store=store, redis=_Redis(), session_id=s.id, org_id=org, agent_id="agent-1",
                       user_id=uuid4(), caller="40722000111")


async def _user_messages(call):
    return [e.data["content"] for e in await call.store.get_events(call.session_id)
            if e.type == EventType.USER_MESSAGE.value]


async def test_send_posts_the_utterance_and_wakes_the_agent(call):
    after = await call.send("Bună ziua")
    assert await _user_messages(call) == ["Bună ziua"]
    assert len(call.redis.queued) == 1 and after > 0


async def test_stream_yields_the_turn_and_stops_at_the_final_answer(call):
    after = await call.send("Cât e euro?")
    emit = call.store.emit_event
    await emit(call.session_id, EventType.LLM_DELTA, {"content": "O clipă, verific. "})
    await emit(call.session_id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "tool_calls": [{"id": "1"}]}})
    await emit(call.session_id, EventType.LLM_DELTA, {"content": "Euro e 4,97 lei."})
    await emit(call.session_id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "Euro e 4,97 lei."}})
    await emit(call.session_id, EventType.LLM_DELTA, {"content": "from the next turn"})
    assert "".join([t async for t in call.stream(after)]) == "O clipă, verific. Euro e 4,97 lei."


async def test_a_question_from_the_agent_is_spoken_and_ends_the_turn(call):
    after = await call.send("Vreau o programare")
    args = {"question": "Ce zi vă convine?", "options": ["luni", "marți"]}
    await call.store.emit_event(call.session_id, EventType.TOOL_CALL,
                                {"tool_call_id": "q1", "name": "ask_user_question", "arguments": json.dumps(args)})
    assert [t async for t in call.stream(after)] == ["Ce zi vă convine? Variante: luni, marți."]


async def test_interrupt_stops_the_turn_without_pausing_the_session(call):
    await call.interrupt()
    channel, payload = call.redis.published[0]
    assert channel == f"surogates:interrupt:{call.session_id}" and payload == {"reason": "channel_stop"}


async def test_barge_in_mid_generation_records_only_what_was_heard(call):
    await call.send("Spune-mi știrile")
    await call.record_heard("Prima știre este despre")
    replies = [e.data for e in await call.store.get_events(call.session_id) if e.type == EventType.LLM_RESPONSE.value]
    assert replies == [{"message": {"role": "assistant", "content": "Prima știre este despre"}, "synthetic": "voice_heard"}]
    await call.send("Altceva")
    assert (await _user_messages(call))[-1] == "Altceva"


async def test_barge_in_after_the_answer_was_written_tells_the_agent_on_the_next_turn(call):
    await call.send("Spune-mi știrile")
    await call.store.emit_event(call.session_id, EventType.LLM_RESPONSE,
                                {"message": {"role": "assistant", "content": "Prima știre... A doua știre..."}})
    await call.record_heard("Prima știre")
    await call.send("Stop")
    assert (await _user_messages(call))[-1] == "[Apelantul te-a întrerupt; din răspunsul tău anterior a auzit doar: «Prima știre».] Stop"


async def test_cut_before_a_word_was_heard(call):
    await call.send("Salut")
    await call.record_heard("")
    await call.send("Alo?")
    assert (await _user_messages(call))[-1] == f"{HEARD_NONE}Alo?"


@pytest.mark.parametrize("raw, expected", [("+40722000111", "40722000111"), ("40722000111", "40722000111"),
                                           ("", "anonymous"), (None, "anonymous"), ("anonymous", "anonymous")])
def test_normalize_caller(raw, expected):
    assert normalize_caller(raw) == expected


async def _open(monkeypatch, *, caller, remember):
    seen = {}

    async def identity(sf, *, platform, platform_user_id, org_id, display_name=""):
        seen["identity"] = (platform, platform_user_id)
        return type("Ident", (), {"user_id": uuid4()})()

    async def session(store, redis, *, session_key, config, channel, **kw):
        seen["session"] = (session_key, channel, config)
        return uuid4()

    monkeypatch.setattr(voice_sessions, "get_or_create_channel_identity", identity)
    monkeypatch.setattr(voice_sessions, "get_or_create_channel_session", session)
    vs = VoiceSessions(store=None, redis=None, session_factory=None)
    target = CallTarget(org_id=uuid4(), agent_id="agent-1", remember_callers=remember)
    call = await vs.open_call(target, call_id="SCL_1", called="+40300000001", caller=caller, greeting="Bună ziua!")
    return call, seen


async def test_open_call_isolates_each_call_by_default(monkeypatch):
    call, seen = await _open(monkeypatch, caller="+40722000111", remember=False)
    key, channel, config = seen["session"]
    assert (key, channel, config["memory_boundary"]) == ("agent:voice:call:SCL_1", "voice", "voice:call:SCL_1")
    assert seen["identity"] == ("voice", "40722000111")


async def test_open_call_remembers_a_known_number_when_the_agent_asks(monkeypatch):
    _, seen = await _open(monkeypatch, caller="+40722000111", remember=True)
    assert seen["session"][2]["memory_boundary"] == "phone:40722000111"


async def test_open_call_withheld_number_never_remembers(monkeypatch):
    call, seen = await _open(monkeypatch, caller=None, remember=True)
    assert seen["identity"] == ("voice", "anonymous")
    assert seen["session"][2]["memory_boundary"] == "voice:call:SCL_1"
    assert call.caller == "anonymous"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_sessions.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.voice.sessions'`

- [ ] **Step 3: Implement**

```python
# surogates/voice/sessions.py
"""A phone call's agent session, driven through the runtime's internals rather than the HTTP API.

One session per call (``channel="voice"``). Each caller utterance is a ``user.message`` plus a
wake on the shared queue; the answer is read back from the event log as it is written, nudged by
the session's pub/sub channel (the nudge carries only an id, so events are re-read).
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from surogates.channels.identity import get_or_create_channel_identity, get_or_create_channel_session
from surogates.channels.inbound import build_principal_stamp
from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.session.events import EventType
from surogates.session.interactive_input import try_resolve_text_answer

TERMINAL = frozenset({"session.complete", "session.fail", "session.stopped", "session.pause"})
POLL_SECONDS = 0.4  # pub/sub is a nudge; poll as a fallback, like the OpenAI route
ANONYMOUS = "anonymous"
HEARD_NONE = "[Apelantul te-a întrerupt înainte să audă răspunsul tău anterior.] "
HEARD_PART = "[Apelantul te-a întrerupt; din răspunsul tău anterior a auzit doar: «{}».] "
GREETED = "[Ai răspuns deja la telefon cu: «{}».] "


def normalize_caller(raw: str | None) -> str:
    """The caller's number as identities store it (no '+'), or ``anonymous`` when it is withheld."""
    digits = (raw or "").strip().lstrip("+")
    return digits if re.fullmatch(r"\d{3,15}", digits) else ANONYMOUS


def question_text(arguments: Any) -> str:
    """An ``ask_user_question`` call as one spoken question; its options are read as a list."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return ""
    if not isinstance(arguments, dict):
        return ""
    question = str(arguments.get("question") or "").strip()
    options = [str(o).strip() for o in arguments.get("options") or [] if str(o).strip()]
    return f"{question} Variante: {', '.join(options)}." if question and options else question


@dataclass(frozen=True)
class CallTarget:
    org_id: UUID
    agent_id: str
    remember_callers: bool = False


@dataclass
class CallSession:
    store: Any
    redis: Any
    session_id: UUID
    org_id: UUID
    agent_id: str
    user_id: UUID
    caller: str
    note: str = ""  # said to the agent before the caller's next words (greeting, what a barge-in cut)
    _user_event: int = 0

    async def send(self, text: str) -> int:
        """Post what the caller said and wake the agent; returns the event id to stream after."""
        answered = await try_resolve_text_answer(self.store, session_id=self.session_id, text=text)
        if answered is not None:  # it answered the agent's pending question; the turn goes on
            self._user_event = answered
            return answered
        content, self.note = f"{self.note}{text}", ""
        if (await self.store.get_session(self.session_id)).status in ("completed", "paused", "failed"):
            await self.store.resume_session(self.session_id, source="voice")
        data = {"content": content, "media_urls": [], "media_types": [],
                "source": {"platform": "voice", "chat_id": self.caller, "chat_type": "dm", "user_id": self.caller,
                           "user_name": self.caller, "thread_id": None}}
        data.update(build_principal_stamp(user_id=self.user_id))
        self._user_event = await self.store.emit_event(self.session_id, EventType.USER_MESSAGE, data)
        await enqueue_session(self.redis, org_id=str(self.org_id), agent_id=self.agent_id, session_id=self.session_id)
        return self._user_event

    async def stream(self, after: int) -> AsyncIterator[str]:
        """The turn's text as the agent writes it. Ends at its final answer, at a question, or when the turn ends."""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"surogates:session:{self.session_id}")
        cursor = after
        try:
            while True:
                for e in await self.store.get_events(self.session_id, after=cursor):
                    cursor, data = e.id, e.data or {}
                    if e.type == EventType.LLM_DELTA.value and data.get("content"):
                        yield data["content"]
                    elif e.type == EventType.TOOL_CALL.value and data.get("name") == "ask_user_question":
                        if question := question_text(data.get("arguments")):
                            yield question
                        return
                    elif e.type == EventType.LLM_RESPONSE.value and not (data.get("message") or {}).get("tool_calls"):
                        return  # the final answer; summaries and completion may follow, nothing more to say
                    elif e.type in TERMINAL:
                        return
                try:
                    await pubsub.get_message(ignore_subscribe_messages=True, timeout=POLL_SECONDS)
                except Exception:
                    await asyncio.sleep(POLL_SECONDS)
        finally:
            await pubsub.aclose()

    async def interrupt(self) -> None:
        """The caller talked over the agent: stop its turn. The session stays active for the next words."""
        await self.redis.publish(f"{INTERRUPT_CHANNEL_PREFIX}:{self.session_id}", json.dumps({"reason": "channel_stop"}))

    async def record_heard(self, heard: str) -> None:
        """Make the history say what the caller actually heard of the answer they cut off.

        Stopped mid-generation, the harness persists no reply, so the heard part becomes the reply.
        If the whole reply was already written, the agent is told on the caller's next words.
        """
        heard = heard.strip()
        events = await self.store.get_events(self.session_id, after=self._user_event)
        written = any(e.type == EventType.LLM_RESPONSE.value and not ((e.data or {}).get("message") or {}).get("tool_calls")
                      for e in events)
        # ponytail: a reply persisted between this read and the stop still lands whole; the next turn's note covers it
        if heard and not written:
            await self.store.emit_event(self.session_id, EventType.LLM_RESPONSE,
                                        {"message": {"role": "assistant", "content": heard}, "synthetic": "voice_heard"})
        else:
            self.note = HEARD_PART.format(heard) if heard else HEARD_NONE


class VoiceSessions:
    def __init__(self, *, store: Any, redis: Any, session_factory: Any, storage: Any = None, settings: Any = None):
        self._store, self._redis, self._sf, self._storage, self._settings = store, redis, session_factory, storage, settings

    async def open_call(self, target: CallTarget, *, call_id: str, called: str, caller: str | None,
                        greeting: str = "") -> CallSession:
        """A fresh session for this call. Memory is the call's own unless the agent remembers known numbers."""
        caller_id = normalize_caller(caller)
        ident = await get_or_create_channel_identity(self._sf, platform="voice", platform_user_id=caller_id,
                                                     org_id=target.org_id,
                                                     display_name=caller_id if caller_id != ANONYMOUS else "apelant anonim")
        remember = target.remember_callers and caller_id != ANONYMOUS
        session_id = await get_or_create_channel_session(
            self._store, self._redis, session_key=f"agent:voice:call:{call_id}", user_id=ident.user_id,
            org_id=target.org_id, agent_id=target.agent_id, channel="voice",
            config={"memory_boundary": f"phone:{caller_id}" if remember else f"voice:call:{call_id}",
                    "voice_call_id": call_id, "voice_called": called, "voice_caller": caller_id, "multi_party": False},
            session_factory=self._sf, storage=self._storage, settings=self._settings)
        return CallSession(store=self._store, redis=self._redis, session_id=session_id, org_id=target.org_id,
                           agent_id=target.agent_id, user_id=ident.user_id, caller=caller_id,
                           note=GREETED.format(greeting) if greeting else "")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_voice_sessions.py -q`
Expected: 15 passed. If `store.create_session` on the in-memory SQLite needs an `orgs` row, insert one in the `call` fixture with the same `org` id (the `sf` fixture already creates the `orgs` table).

- [ ] **Step 5: Commit**

```bash
git add surogates/voice/sessions.py tests/test_voice_sessions.py
git commit -F - <<'EOF'
feat(voice): drive a call's agent session from inside the runtime

One session per call: post the caller's words, stream the turn's text from
the event log, stop the turn on a barge-in, and record what the caller
actually heard so the next turn starts from the truth.
EOF
```

---

### Task 7: SurogatesLLM — the agent as LiveKit's LLM

**Files:**
- Create: `surogates/voice/llm.py`
- Test: `tests/test_voice_llm.py`

**Interfaces:**
- Consumes: `CallSession.send/stream/interrupt` (Task 6).
- Produces: `class SurogatesLLM(llm.LLM)(call: CallSession)`; `latest_user_text(chat_ctx: llm.ChatContext) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice_llm.py
"""LiveKit's LLM slot speaks to the call's session: one utterance in, the turn's text out, a barge-in stops it."""
import asyncio

from livekit.agents import llm

from surogates.voice.llm import SurogatesLLM


class _Call:
    def __init__(self, pieces, hang=False):
        self.pieces, self.hang = pieces, hang
        self.sent, self.interrupted = [], False

    async def send(self, text):
        self.sent.append(text)
        return 7

    async def stream(self, after):
        assert after == 7
        for p in self.pieces:
            yield p
        if self.hang:
            await asyncio.Event().wait()

    async def interrupt(self):
        self.interrupted = True


def _ctx(*turns):
    ctx = llm.ChatContext.empty()
    for role, text in turns:
        ctx.add_message(role=role, content=text)
    return ctx


async def test_sends_only_the_latest_utterance_and_streams_the_answer():
    call = _Call(["Euro ", "e 4,97 lei."])
    ctx = _ctx(("assistant", "Bună ziua!"), ("user", "Salut"), ("assistant", "Salut!"), ("user", "Cât e euro?"))
    async with SurogatesLLM(call).chat(chat_ctx=ctx) as stream:
        text = "".join([c.delta.content async for c in stream if c.delta and c.delta.content])
    assert call.sent == ["Cât e euro?"] and text == "Euro e 4,97 lei." and not call.interrupted


async def test_closing_mid_answer_stops_the_agents_turn():
    call = _Call(["Prima știre "], hang=True)
    stream = SurogatesLLM(call).chat(chat_ctx=_ctx(("user", "Știrile?")))
    async for chunk in stream:
        break
    await stream.aclose()
    assert call.interrupted
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_llm.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.voice.llm'`

- [ ] **Step 3: Implement**

```python
# surogates/voice/llm.py
"""The call's agent session in LiveKit's LLM slot.

The session keeps the conversation server-side, so each turn sends only the caller's latest
words; LiveKit's own chat context is display only. Tools run in the harness, never here. A turn
cancelled by a barge-in stops the agent's turn too.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr, llm

from surogates.voice.sessions import CallSession


def latest_user_text(chat_ctx: llm.ChatContext) -> str:
    for item in reversed(chat_ctx.items):
        if getattr(item, "role", None) == "user":
            return (item.text_content or "").strip()
    return ""


class SurogatesLLM(llm.LLM):
    def __init__(self, call: CallSession) -> None:
        super().__init__()
        self._call = call

    @property
    def model(self) -> str:
        return "surogates-agent"

    @property
    def provider(self) -> str:
        return "surogates"

    def chat(self, *, chat_ctx: llm.ChatContext, tools: list[llm.Tool] | None = None,
             conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
             parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN, tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
             extra_kwargs: NotGivenOr[dict] = NOT_GIVEN) -> SurogatesStream:
        # never retried: a retry would post the caller's words twice
        return SurogatesStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=replace(conn_options, max_retry=0))


class SurogatesStream(llm.LLMStream):
    async def _run(self) -> None:
        text = latest_user_text(self._chat_ctx)
        if not text:
            return
        call: CallSession = self._llm._call
        after = await call.send(text)
        finished = False
        try:
            async for piece in call.stream(after):
                self._event_ch.send_nowait(llm.ChatChunk(id=str(after),
                                                         delta=llm.ChoiceDelta(role="assistant", content=piece)))
            finished = True
        finally:
            if not finished:
                await asyncio.shield(call.interrupt())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_voice_llm.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add surogates/voice/llm.py tests/test_voice_llm.py
git commit -F - <<'EOF'
feat(voice): put the agent's session in LiveKit's LLM slot

Each turn sends only the caller's latest words; a turn LiveKit cancels for a
barge-in stops the agent's turn as well. Never retried.
EOF
```

---

### Task 8: VoiceAgent and per-call settings

**Files:**
- Create: `surogates/voice/agent.py`
- Test: `tests/test_voice_agent.py`

**Interfaces:**
- Consumes: `spoken_sentences`, `say_as`, `is_echo`, `caller_says_goodbye`, `is_farewell` (Task 1).
- Produces:
  - `CallConfig` (frozen dataclass: `greeting: str`, `voice: str`, `pronunciations: Mapping[str, str]`, `remember_callers: bool`, `max_call_seconds: float`, `idle_ask_seconds: float`, `idle_hangup_seconds: float`) with `CallConfig.from_routing(cfg: Any) -> CallConfig`
  - constants `STILL_THERE`, `GOODBYE`, `SORRY`, `UNAVAILABLE`, `TURN_HANDLING`
  - `class VoiceAgent(Agent)(config: CallConfig)` with attribute `hangup_after_reply: bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice_agent.py
"""Per-call settings come from the ops routing row and survive whatever is in it."""
from surogates.voice.agent import GREETING_DEFAULT, CallConfig


def test_call_config_reads_the_routing_row():
    cfg = CallConfig.from_routing({"greeting": "Salut, sunt Ana.", "voice": "male", "remember_callers": True,
                                   "pronunciations": {"Nvidia": "Envidia"}, "max_call_seconds": 300})
    assert (cfg.greeting, cfg.voice, cfg.remember_callers, dict(cfg.pronunciations), cfg.max_call_seconds) == \
        ("Salut, sunt Ana.", "male", True, {"Nvidia": "Envidia"}, 300.0)


def test_call_config_survives_bad_values():
    cfg = CallConfig.from_routing({"greeting": "   ", "voice": "robot", "remember_callers": "yes",
                                   "pronunciations": ["Nvidia"], "max_call_seconds": 10 ** 9,
                                   "idle_ask_seconds": True, "unknown": object()})
    assert cfg == CallConfig()
    assert CallConfig.from_routing(None) == CallConfig() and CallConfig().greeting == GREETING_DEFAULT
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_agent.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.voice.agent'`

- [ ] **Step 3: Implement**

```python
# surogates/voice/agent.py
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
from livekit.agents import Agent, ModelSettings, StopResponse, llm

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

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        text, now = new_message.text_content or "", time.monotonic()
        self.recent = [(t, s) for t, s in self.recent if now - t < ECHO_WINDOW]
        if is_echo(text, [s for _, s in self.recent]):
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_voice_agent.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add surogates/voice/agent.py tests/test_voice_agent.py
git commit -F - <<'EOF'
feat(voice): the call's speaking side, with settings from the routing row

VoiceAgent speaks sentence by sentence with a breath between them, ignores
our own echo, and knows when the conversation said goodbye. CallConfig
falls back to defaults on any bad value instead of failing a call.
EOF
```

---

### Task 9: The `surogates voice` process

**Files:**
- Create: `surogates/voice/worker.py`
- Modify: `surogates/config.py` (add `VoiceSettings` before `class Settings`, field `voice` in `Settings` after `channels` at :792)
- Modify: `surogates/cli/main.py` (`cmd_voice`, parser entry, `COMMANDS`)
- Test: `tests/test_voice_worker.py`

**Interfaces:**
- Consumes: everything above; `resolve_tenant(cache, kind, identifier)` (`channels/resolve.py:14`), `build_channel_routing_cache(settings=, platform_client=)` (`api/app.py:371`), `PlatformClient(base_url=, token=)`, `async_engine_from_settings`, `async_session_factory`, `SessionStore`, `create_backend`.
- Produces: `call_info(room_name: str, attributes: Mapping[str, str]) -> CallInfo | None`; `run_voice(settings) -> None`; CLI `surogates voice`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice_worker.py
"""Which of our numbers was called, and by whom, from the SIP participant LiveKit puts in the room."""
from surogates.voice.worker import CallInfo, call_info


def test_call_info_from_sip_attributes():
    attrs = {"sip.trunkPhoneNumber": "+40300000001", "sip.phoneNumber": "+40722000111", "sip.callID": "SCL_abc"}
    assert call_info("call-_+40722000111_x", attrs) == CallInfo(call_id="SCL_abc", called="+40300000001",
                                                                 caller="+40722000111")


def test_call_info_normalizes_the_called_number_and_tolerates_a_withheld_caller():
    assert call_info("room-1", {"sip.trunkPhoneNumber": "40300000001"}) == \
        CallInfo(call_id="room-1", called="+40300000001", caller=None)


def test_call_info_not_a_phone_call():
    assert call_info("room-1", {}) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_voice_worker.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.voice.worker'`

- [ ] **Step 3: Add the settings**

In `surogates/config.py`, before `class Settings(BaseSettings):`:

```python
class VoiceSettings(BaseSettings):
    """Phone calls through LiveKit SIP (``surogates voice``).

    ``stt_url`` / ``tts_url`` are our Romanian speech services; in development
    they are the prod ones through an SSH tunnel. ``max_calls`` is per process:
    LiveKit sends no more jobs once it is reached.
    """

    model_config = {"env_prefix": "SUROGATES_VOICE_"}

    livekit_url: str = "ws://127.0.0.1:7880"
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    agent_name: str = "surogate-voice"
    stt_url: str = "ws://127.0.0.1:18001/v1/audio/streams"
    tts_url: str = "http://127.0.0.1:18080/v1/audio/speech"
    max_calls: int = 8
    health_port: int = 8003
```

In `class Settings`, after `channels: ChannelsSettings = Field(default_factory=ChannelsSettings)`:

```python
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
```

- [ ] **Step 4: Implement the worker**

```python
# surogates/voice/worker.py
"""``surogates voice``: answers phone calls (LiveKit SIP) with the agent their number belongs to.

One LiveKit job per call, each in its own process. A job reads which of our numbers was called,
resolves it through the ops channel routing (``voice:+40…``), opens the call's session and runs
the conversation until someone hangs up.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from livekit.agents import Agent, AgentServer, AgentSession, AudioConfig, BackgroundAudioPlayer, BuiltinAudioClip, JobContext, JobProcess
from livekit.plugins import silero
from redis.asyncio import Redis

from surogates.channels.resolve import resolve_tenant
from surogates.config import load_settings
from surogates.voice.agent import GOODBYE, SORRY, STILL_THERE, TURN_HANDLING, UNAVAILABLE, CallConfig, VoiceAgent
from surogates.voice.llm import SurogatesLLM
from surogates.voice.sessions import CallTarget, VoiceSessions
from surogates.voice.stt import RoSTT
from surogates.voice.tts import RoTTS

log = logging.getLogger("surogates.voice")


@dataclass(frozen=True)
class CallInfo:
    call_id: str
    called: str
    caller: str | None


def call_info(room_name: str, attributes: Mapping[str, str]) -> CallInfo | None:
    """Who called which of our numbers, from the SIP participant's attributes. ``None``: not a phone call."""
    called = (attributes.get("sip.trunkPhoneNumber") or "").strip()
    if not called:
        return None
    return CallInfo(call_id=attributes.get("sip.callID") or room_name,
                    called=called if called.startswith("+") else f"+{called}",
                    caller=(attributes.get("sip.phoneNumber") or "").strip() or None)


@dataclass
class Runtime:
    """What one call needs from the platform: DB, Redis, ops routing. Built per job process."""

    engine: Any
    redis: Any
    routing: Any
    sessions: VoiceSessions

    @classmethod
    async def open(cls, settings: Any) -> Runtime:
        from surogates.api.app import build_channel_routing_cache
        from surogates.db.engine import async_engine_from_settings, async_session_factory
        from surogates.runtime.platform_client import PlatformClient
        from surogates.session.store import SessionStore
        from surogates.storage.backend import create_backend

        engine = async_engine_from_settings(settings.db)
        sf = async_session_factory(engine)
        redis = Redis.from_url(settings.redis.url)
        client = PlatformClient(base_url=settings.platform_api_url, token=settings.platform_api_token)
        routing = build_channel_routing_cache(settings=settings, platform_client=client)
        sessions = VoiceSessions(store=SessionStore(sf, redis=redis), redis=redis, session_factory=sf,
                                 storage=create_backend(settings), settings=settings)
        return cls(engine=engine, redis=redis, routing=routing, sessions=sessions)

    async def aclose(self) -> None:
        await self.redis.aclose()
        await self.engine.dispose()


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def say_and_hang_up(ctx: JobContext, session: AgentSession, text: str) -> None:
    await session.say(text, allow_interruptions=False, add_to_chat_ctx=False).wait_for_playout()
    await ctx.delete_room()


async def entrypoint(ctx: JobContext) -> None:
    settings = load_settings()
    vs = settings.voice
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    info = call_info(ctx.room.name, participant.attributes)
    rt = await Runtime.open(settings)
    ctx.add_shutdown_callback(rt.aclose)

    tenant = await resolve_tenant(rt.routing, "voice", info.called) if info else None
    if info is None or tenant is None:
        log.warning("no agent for room %s (called %s)", ctx.room.name, info and info.called)
        bare = AgentSession(tts=RoTTS(url=vs.tts_url))
        await bare.start(agent=Agent(instructions=""), room=ctx.room)
        return await say_and_hang_up(ctx, bare, UNAVAILABLE)

    config = CallConfig.from_routing(tenant.get("config"))
    try:
        call = await rt.sessions.open_call(
            CallTarget(org_id=UUID(str(tenant["org_id"])), agent_id=tenant["agent_id"],
                       remember_callers=config.remember_callers),
            call_id=info.call_id, called=info.called, caller=info.caller, greeting=config.greeting)
    except Exception:
        log.exception("could not open a session for call %s", info.call_id)
        bare = AgentSession(tts=RoTTS(url=vs.tts_url, voice=config.voice))
        await bare.start(agent=Agent(instructions=""), room=ctx.room)
        return await say_and_hang_up(ctx, bare, SORRY)
    log.info("call %s to %s from %s -> agent %s session %s", info.call_id, info.called, call.caller,
             tenant["agent_id"], call.session_id)

    agent = VoiceAgent(config)
    session = AgentSession(vad=ctx.proc.userdata["vad"], stt=RoSTT(url=vs.stt_url), llm=SurogatesLLM(call),
                           tts=RoTTS(url=vs.tts_url, voice=config.voice), turn_handling=TURN_HANDLING,
                           user_away_timeout=config.idle_ask_seconds)
    tasks: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @session.on("conversation_item_added")
    def _heard(ev) -> None:
        item = ev.item
        if getattr(item, "role", None) == "assistant" and getattr(item, "interrupted", False):
            spawn(call.record_heard(item.text_content or ""))

    @session.on("agent_state_changed")
    def _done_speaking(ev) -> None:
        if ev.new_state == "listening" and agent.hangup_after_reply:
            agent.hangup_after_reply = False
            spawn(ctx.delete_room())

    async def still_there() -> None:
        await session.say(STILL_THERE, add_to_chat_ctx=False).wait_for_playout()
        await asyncio.sleep(config.idle_hangup_seconds)
        if session.user_state == "away":
            await say_and_hang_up(ctx, session, GOODBYE)

    @session.on("user_state_changed")
    def _away(ev) -> None:
        if ev.new_state == "away" and session.agent_state == "listening":
            spawn(still_there())

    async def time_limit() -> None:
        await asyncio.sleep(config.max_call_seconds)
        await say_and_hang_up(ctx, session, GOODBYE)

    async def cancel_tasks() -> None:
        for task in list(tasks):
            task.cancel()

    ctx.add_shutdown_callback(cancel_tasks)
    await session.start(agent=agent, room=ctx.room)
    background = BackgroundAudioPlayer(thinking_sound=[AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.5)])
    await background.start(room=ctx.room, agent_session=session)
    session.say(config.greeting)
    spawn(time_limit())


async def run_voice(settings: Any) -> None:
    vs = settings.voice
    server = AgentServer(ws_url=vs.livekit_url, api_key=vs.livekit_api_key, api_secret=vs.livekit_api_secret,
                         setup_fnc=prewarm, port=vs.health_port, load_threshold=1.0,
                         load_fnc=lambda s: len(s.active_jobs) / max(vs.max_calls, 1))
    server.rtc_session(entrypoint, agent_name=vs.agent_name)
    await server.run()
```

- [ ] **Step 5: Add the CLI command**

In `surogates/cli/main.py`, next to `cmd_channels`:

```python
def cmd_voice(args: argparse.Namespace) -> None:
    """Answer phone calls through LiveKit SIP (needs the ``voice`` extra)."""
    from surogates.config import load_settings
    from surogates.voice.worker import run_voice

    settings = load_settings()
    _configure_logging(settings.log_level)
    logging.getLogger("surogates.voice").info(
        "Starting voice worker %r against %s", settings.voice.agent_name, settings.voice.livekit_url,
    )
    asyncio.run(run_voice(settings))
```

In `build_parser`, after the `channels` parser:

```python
    # surogate voice
    sub.add_parser("voice", help="Answer phone calls through LiveKit SIP")
```

In `COMMANDS`, add `"voice": cmd_voice,`.

- [ ] **Step 6: Run the tests and an import smoke check**

Run: `.venv/bin/python -m pytest tests/test_voice_worker.py -q && .venv/bin/surogates voice --help`
Expected: 3 passed; the help text prints. Then run the whole voice suite plus the channel tests: `.venv/bin/python -m pytest tests -q -k "voice or channel"` — all pass.

- [ ] **Step 7: Commit**

```bash
git add surogates/voice/worker.py surogates/config.py surogates/cli/main.py tests/test_voice_worker.py
git commit -F - <<'EOF'
feat(voice): `surogates voice` answers calls with the number's agent

One LiveKit job per call: resolve the called number through ops routing,
open the call's session, greet, converse, and hang up after a goodbye, on
silence or at the time limit. A number nobody owns hears that and ends.
EOF
```

---

### Task 10: Local end-to-end loop

**Files:**
- Create: `scripts/voice-dev/README.md`
- Create: `scripts/voice-dev/livekit-up.sh`
- Create: `scripts/voice-dev/sip.yaml`

**Interfaces:**
- Consumes: `DEV_MODE=local ./dev.sh start` (workspace root), the speech tunnel (`.devstack/speech-tunnel.pid`), `surogates voice`.
- Produces: a reproducible way to place a call against the local stack.

- [ ] **Step 1: Write the LiveKit dev script**

```yaml
# scripts/voice-dev/sip.yaml — local livekit-sip; dev keys only
api_key: devkey
api_secret: secret
ws_url: ws://livekit:7880
redis:
  address: lk-redis:6379
sip_port: 5060
rtp_port: 10000-10100
use_external_ip: false
health_port: 8080
logging:
  level: info
```

```bash
#!/usr/bin/env bash
# scripts/voice-dev/livekit-up.sh — LiveKit server + SIP + Redis in Docker, with the dev trunk and dispatch rule.
# Dev keys (devkey/secret) only; nothing here talks to prod.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DID="${VOICE_DEV_DID:-+40300000001}"
docker network inspect lkdev >/dev/null 2>&1 || docker network create lkdev >/dev/null
docker run -d --rm --name lk-redis --network lkdev redis:7-alpine >/dev/null
docker run -d --rm --name livekit --network lkdev -p 127.0.0.1:7880:7880 \
  -e LIVEKIT_CONFIG="redis: {address: lk-redis:6379}" livekit/livekit-server:latest --dev --bind 0.0.0.0 >/dev/null
docker run -d --rm --name lk-sip --network lkdev -p 127.0.0.1:5060:5060/udp -p 127.0.0.1:10000-10100:10000-10100/udp \
  -v "$HERE/sip.yaml:/sip/config.yaml:ro" livekit/sip:latest >/dev/null
TMP="$(mktemp -d)"
lk() { docker run --rm --network lkdev -v "$TMP:/w" -w /w livekit/livekit-cli --url http://livekit:7880 --api-key devkey --api-secret secret "$@"; }
sleep 3
# JSON request files: the shape verified against livekit-sip on 2026-10-05
printf '{"trunk": {"name": "dev-inbound", "numbers": ["%s"]}}\n' "$DID" > "$TMP/inbound.json"
TRUNK=$(lk sip inbound create inbound.json | awk '/SIPTrunkID/{print $2}')
printf '{"dispatch_rule": {"name": "dev-voice", "trunk_ids": ["%s"], "rule": {"dispatchRuleIndividual": {"roomPrefix": "call-"}}, "room_config": {"agents": [{"agent_name": "surogate-voice"}]}}}\n' "$TRUNK" > "$TMP/dispatch.json"
lk sip dispatch create dispatch.json >/dev/null
echo "LiveKit ws://127.0.0.1:7880 (devkey/secret), SIP udp 127.0.0.1:5060, trunk $TRUNK for $DID"
echo "stop: docker rm -f lk-sip livekit lk-redis && docker network rm lkdev"
```

- [ ] **Step 2: Write the README with the exact loop**

````markdown
<!-- scripts/voice-dev/README.md -->
# Calling the agent locally

Everything runs on the laptop except the Romanian speech models, which are the prod ones through
an SSH tunnel. Nothing here changes prod.

1. Local stack: from the workspace root, `DEV_MODE=local ./dev.sh start`.
2. Speech tunnel to the prod speech services (command in the private runbook):
   `the speech tunnel command from the private runbook (workspace AGENTS.md, "Voice channel locally")`
   Check: `curl -s http://127.0.0.1:18080/v1/audio/voices` lists `female` and `male`.
3. LiveKit: `scripts/voice-dev/livekit-up.sh`.
4. A routing row so the number reaches an agent (local ops DB only):
   ```sql
   -- docker exec -it sg-pg psql -U <user> -d surogate
   \d channel_routing   -- confirm the columns below before inserting
   INSERT INTO channel_routing (channel_kind, channel_identifier, agent_id, org_id, config, active)
   VALUES ('voice', '+40300000001', '<agent id>', '<org/project id of that agent>',
           '{"greeting": "Bună ziua! Sunt asistentul de test. Cu ce vă pot ajuta?", "voice": "female"}', true);
   ```
5. The worker: `cd surogates && SUROGATES_CONFIG=~/.surogate/surogates.local.yaml SUROGATES_VOICE_LIVEKIT_API_KEY=devkey SUROGATES_VOICE_LIVEKIT_API_SECRET=secret .venv/bin/surogates voice`
   (it also needs `SUROGATES_PLATFORM_API_URL` / `_TOKEN` from `~/.surogate/local-only-runtime-token.env`).
6. Call `sip:+40300000001@127.0.0.1:5060` from a softphone (Linphone, UDP, no account; G.711).
   livekit-sip announces its container IP for RTP; OrbStack routes container IPs from the Mac, so audio
   flows without extra setup (on Docker Desktop it would not — use OrbStack).
   Expected: the greeting within about a second of answering; replies in Romanian; the
   transcript appears in Studio as a `voice` session of that agent.
7. Checks, each with a fresh call:
   - Talk over a long answer: it stops within ~0.6 s; ask again — the agent knows where it was cut.
   - Say "da" / "aha" while it talks: it keeps talking.
   - Say "Mulțumesc, atât.": one farewell sentence, then the call ends.
   - Stay silent 30 s: "Mai sunteți acolo?", then goodbye after 15 s more.
   - Call a number with no routing row (`VOICE_DEV_DID=+40371000000` for a second trunk): "Acest număr nu este disponibil", call ends.
8. Stop: `docker rm -f lk-sip livekit lk-redis && docker network rm lkdev`, `DEV_MODE=local ./dev.sh stop`,
   `kill $(cat .devstack/speech-tunnel.pid)`.
````

- [ ] **Step 3: Run the loop**

Run steps 1–7 of the README. Expected: every check in step 7 behaves as written. Record in the PR description the measured time from the end of your sentence to the agent's first audio for three turns (the worker logs LiveKit's `metrics_collected`; until turn metrics land in Phase 2, use a stopwatch).

- [ ] **Step 4: Commit**

```bash
chmod +x scripts/voice-dev/livekit-up.sh
git add scripts/voice-dev
git commit -F - <<'EOF'
chore(voice): a local loop for calling the agent from a softphone

LiveKit server and SIP in Docker with a dev trunk and dispatch rule, the
prod speech models through an SSH tunnel, and the checks to run per call.
EOF
```

---

## Self-review notes

- Spec §3 steps 1 (admission beyond `max_calls`), 3 (cached greeting PCM), 5 (tool-start typing driven by events — v1 uses LiveKit's thinking sound), 8 (`end_call`/`transfer_call` tools), §5 turn metrics and harness warm path, and §8–9 (ops, prod) are Phase 2–4 plans, not this one.
- Types used across tasks: `CallSession.send -> int`, `stream(after: int) -> AsyncIterator[str]`, `CallConfig.from_routing`, `call_info -> CallInfo | None` — consistent in Tasks 6–9.
