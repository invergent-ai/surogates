"""The call's background sound: what a caller hears around a person answering at a real desk.

Three layers, all set relative to the agent's voice so they never compete with it:

- the room: a faint loop of the place, a little louder behind the voice and almost gone in silence,
  the way a headset microphone with noise gating sounds;
- events: rare distant sounds of the place (a door, a phone in another room), never as a phrase starts;
- actions: short sounds caused by what the agent does — typing and clicks while it checks something,
  a pen while the caller gives a name — and hold music for a long wait, if the agent has it.

Nothing plays while the agent talks except the room, and the caller speaking cuts actions at once
(a barge-in must reach the speech recogniser clean). The clips come from a sound pack that ops
serves (``manifest.json`` plus audio); this module holds no audio of its own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("surogates.voice")

# dB relative to the agent's voice
ROOM_TALKING, ROOM_QUIET, ROOM_LOW = -34.0, -44.0, -6.0
EVENT, ACTION, PEN, CHAIR, HOLD = -30.0, -19.0, -22.0, -25.0, -24.0
JITTER = 3.0  # ± dB on every one-shot: the same sound is never exactly the same

ROOM_ATTACK, ROOM_RELEASE = 0.12, 0.45  # seconds: how fast the room follows the voice
PICKUP, HANGUP, CUT = 0.6, 0.8, 0.06  # fade in, fade out, and the fade of a cut sound
CHECK_AFTER = 0.8  # thinking this long before checking sounds start: quick replies stay clean
HOLD_AFTER = 8.0  # checking this long before hold music, when the agent has it
WRITE_AFTER = 0.8  # the caller starts answering; the pen follows
ONSET_GUARD = 0.6  # no distant event in the first moments of a phrase
EVENT_GAP = {"rare": 28.0, "normal": 14.0}  # mean seconds between events
ACTION_GAP = (0.2, 0.9)


@dataclass(frozen=True)
class SoundSettings:
    """``sound`` in the voice routing config. ``scene=None`` is Silent: nothing plays at all."""

    scene: str | None = None
    room: str = "normal"
    events: str = "rare"
    actions: bool = True
    hold: str | None = None

    @property
    def silent(self) -> bool:
        return self.scene is None

    @classmethod
    def from_routing(cls, cfg: Any) -> SoundSettings:
        """A bad value falls back to its default: a call never fails on its background."""
        cfg, d = (cfg if isinstance(cfg, dict) else {}), cls()

        def ident(key: str) -> str | None:
            v = cfg.get(key)
            return v if isinstance(v, str) and v.strip() else None

        def choice(key: str, allowed: Iterable[str]) -> str:
            v = cfg.get(key)
            return v if v in allowed else getattr(d, key)

        actions = cfg.get("actions")
        return cls(scene=ident("scene"), room=choice("room", ("off", "low", "normal")),
                   events=choice("events", ("off", "rare", "normal")),
                   actions=actions if isinstance(actions, bool) else d.actions, hold=ident("hold"))


def decode_av(path: Path, rate: int) -> np.ndarray:
    """Any audio file as mono float32 at ``rate`` (PyAV ships with livekit-agents)."""
    import av

    out = []
    with av.open(str(path)) as f:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)
        for frame in f.decode(audio=0):
            out.extend(r.to_ndarray().reshape(-1) for r in resampler.resample(frame))
        out.extend(r.to_ndarray().reshape(-1) for r in resampler.resample(None))
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)


@dataclass
class Pack:
    """The clips one call needs. Every clip is stored at the same loudness (``level_dbfs`` RMS)."""

    rate: int
    level_dbfs: float
    beds: dict[str, np.ndarray] = field(default_factory=dict)
    events: dict[str, list[list[np.ndarray]]] = field(default_factory=dict)  # scene → kinds → variants
    actions: dict[str, list[np.ndarray]] = field(default_factory=dict)  # keys, click, pen, …
    hold: dict[str, np.ndarray] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path, settings: SoundSettings, *, rate: int = 48000,
             decode: Callable[[Path, int], np.ndarray] = decode_av) -> Pack:
        """Only what ``settings`` uses; an unknown scene or hold loads nothing for it."""
        m = json.loads((directory / "manifest.json").read_text())
        pack = cls(rate=rate, level_dbfs=float(m["level_dbfs"]))
        if settings.silent:
            return pack
        get = lambda name: decode(directory / name, rate)  # noqa: E731
        scene = next((s for s in m["scenes"] if s["id"] == settings.scene), None)
        if scene is not None:
            pack.beds[scene["id"]] = get(scene["bed"])
            pack.events[scene["id"]] = [[get(f) for f in files] for files in scene["events"].values()]
        if settings.actions:
            pack.actions = {kind: [get(f) for f in files] for kind, files in m["actions"].items()}
        hold = next((h for h in m.get("hold", []) if h["id"] == settings.hold), None)
        if hold is not None:
            pack.hold[hold["id"]] = get(hold["file"])
        return pack


def pack_files(manifest: Mapping[str, Any], settings: SoundSettings) -> list[str]:
    """The files of a pack that ``settings`` needs (what a worker downloads)."""
    if settings.silent:
        return []
    files = []
    for s in manifest["scenes"]:
        if s["id"] == settings.scene:
            files += [s["bed"], *(f for kind in s["events"].values() for f in kind)]
    if settings.actions:
        files += [f for kind in manifest["actions"].values() for f in kind]
    files += [h["file"] for h in manifest.get("hold", []) if h["id"] == settings.hold]
    return files


SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*\.(ogg|mp3|json)$")


async def fetch_pack(get: Callable[[str], Any], cache: Path, settings: SoundSettings) -> Path | None:
    """Download what a call needs into ``cache`` (files are content-named, so a cached one is never stale).

    ``get(name)`` is an awaitable returning the file's bytes. ``None`` when the pack is unavailable:
    the call then simply has no background.
    """
    if settings.silent:
        return None
    try:
        manifest = json.loads(await get("manifest.json"))
        cache.mkdir(parents=True, exist_ok=True)
        for name in pack_files(manifest, settings):
            if not SAFE_NAME.match(name):
                raise ValueError(f"unsafe file name in the sound pack: {name!r}")
            path = cache / name
            if not path.exists():
                tmp = path.with_suffix(path.suffix + ".part")
                tmp.write_bytes(await get(name))
                tmp.replace(path)
        (cache / "manifest.json").write_text(json.dumps(manifest))
        return cache
    except Exception:
        log.warning("sound pack unavailable; the call has no background", exc_info=True)
        return None


@dataclass
class _Voice:
    clip: np.ndarray
    gain: float
    kind: str  # "event", "action" or "hold"
    pos: int = 0
    loop: bool = False
    fade: int = 0  # samples of fade-in left
    fade_len: int = 0
    stop_left: int | None = None  # samples left of a fade-out
    stop_len: int = 1


def db(v: float) -> float:
    return 10 ** (v / 20)


class Soundscape:
    """Mixes one call's background, 20 ms at a time. Driven by the call's events; pure and deterministic
    for a given seed, so the same code renders Studio's preview calls and the live line."""

    def __init__(self, pack: Pack, settings: SoundSettings, *, voice_dbfs: float = -20.0,
                 seed: int | None = None) -> None:
        self.pack, self.settings, self.rate = pack, settings, pack.rate
        self.n = pack.rate // 50
        self.rng = random.Random(seed)
        self._base = voice_dbfs - pack.level_dbfs  # dB to bring a stored clip to the voice's level
        self.t = 0
        self._voices: list[_Voice] = []
        self._last: dict[str, int] = {}
        self._speaking = self._caller = False
        self._spoke_at: int | None = None
        self._thinking_since: int | None = None
        self._next_action = 0
        self._first_step = False
        self._holding = False
        self._pen_at: int | None = None
        self._ending: int | None = None
        scene = settings.scene
        self._bed = pack.beds.get(scene) if scene and settings.room != "off" else None
        self._bed_pos = self.rng.randrange(len(self._bed)) if self._bed is not None and len(self._bed) else 0
        self._room = self._room_target()
        self._events = pack.events.get(scene, []) if scene and settings.events != "off" else []
        self._next_event = self._event_gap()

    # --- what happens on the call ---------------------------------------------------------------

    def agent_speaking(self, on: bool) -> None:
        if on and not self._speaking:
            self._spoke_at = self.t
            self._thinking_since = None
            self._holding = False
            self._cut("action", "hold")
        self._speaking = on

    def agent_thinking(self, on: bool) -> None:
        if on and self._thinking_since is None and not self._speaking:
            self._thinking_since = self.t
            self._next_action = self.t + int(CHECK_AFTER * self.rate)
            self._first_step = True
        elif not on:
            self._thinking_since = None
            self._holding = False
            self._cut("action", "hold")

    def caller_speaking(self, on: bool) -> None:
        if on:
            self._cut("action", "hold")
            self._holding = False
            if self._thinking_since is not None:  # they talk over the checking: it resumes after them
                self._thinking_since = self.t
        else:
            self._next_action = max(self._next_action, self.t + int(CHECK_AFTER * self.rate))
        self._caller = on

    def writing(self) -> None:
        if self.settings.actions and "pen" in self.pack.actions:
            self._pen_at = self.t + int(WRITE_AFTER * self.rate)

    def end(self) -> None:
        if self._ending is None:
            self._ending = self.t

    # --- mixing ---------------------------------------------------------------------------------

    def frame(self) -> np.ndarray:
        n, out = self.n, np.zeros(self.n, np.float32)
        if self.settings.silent:
            self.t += n
            return out
        self._schedule()
        if self._bed is not None:
            out += self._bed_block(n) * self._room_ramp(n)
        for v in list(self._voices):
            self._mix(v, out)
        out *= self._master(n)
        self.t += n
        return out

    def _schedule(self) -> None:
        t = self.t
        if self._pen_at is not None and t >= self._pen_at:
            self._pen_at = None
            self._play("action", self._pick("pen"), PEN)
        thinking = self._thinking_since is not None and not self._speaking and not self._caller
        if thinking and self.settings.hold in self.pack.hold and not self._holding \
                and t - self._thinking_since >= int((CHECK_AFTER + HOLD_AFTER) * self.rate):
            self._holding = True
            self._cut("action")
            self._play("hold", self.pack.hold[self.settings.hold], HOLD, loop=True, fade=1.5, jitter=False)
        if thinking and not self._holding and self.settings.actions and self.pack.actions \
                and t >= self._next_action and not any(v.kind == "action" and v.stop_left is None for v in self._voices):
            clip, level = self._next_step()
            self._play("action", clip, level)
            self._next_action = t + len(clip) + int(self.rng.uniform(*ACTION_GAP) * self.rate)
        if self._events and t >= self._next_event:
            if self._spoke_at is not None and self._speaking and t - self._spoke_at < ONSET_GUARD * self.rate:
                self._next_event = self._spoke_at + int(ONSET_GUARD * self.rate)
            else:
                kind = self.rng.randrange(len(self._events))
                self._play("event", self._pick_from(f"event{kind}", self._events[kind]), EVENT)
                self._next_event = self._event_gap()

    def _next_step(self) -> tuple[np.ndarray, float]:
        """One step of someone checking something: mostly keys and clicks, now and then a page or the chair."""
        a, r = self.pack.actions, self.rng.random()
        first, self._first_step = self._first_step, False
        if first and "chair" in a and r < 0.35:
            return self._pick("chair"), CHAIR
        if "page" in a and r < 0.12:
            return self._pick("page"), ACTION
        if "click" in a and r < 0.42:
            return self._pick("click"), ACTION
        if "keys-enter" in a and r < 0.55:
            return self._pick("keys-enter"), ACTION
        return self._pick("keys"), ACTION

    def _pick(self, kind: str) -> np.ndarray:
        return self._pick_from(kind, self.pack.actions[kind])

    def _pick_from(self, key: str, variants: list[np.ndarray]) -> np.ndarray:
        """A variant, never the same one twice in a row."""
        choices = [i for i in range(len(variants)) if i != self._last.get(key)] or [0]
        i = self.rng.choice(choices)
        self._last[key] = i
        return variants[i]

    def _play(self, kind: str, clip: np.ndarray, level: float, *, loop: bool = False,
              fade: float = 0.0, jitter: bool = True) -> None:
        j = self.rng.uniform(-JITTER, JITTER) if jitter else 0.0
        f = int(fade * self.rate)
        self._voices.append(_Voice(clip=clip, gain=db(self._base + level + j), kind=kind, loop=loop,
                                   fade=f, fade_len=max(f, 1)))

    def _cut(self, *kinds: str) -> None:
        for v in self._voices:
            if v.kind in kinds and v.stop_left is None:
                v.stop_left = v.stop_len = max(int(CUT * self.rate), 1) if v.kind == "action" else int(HANGUP * self.rate)

    def _mix(self, v: _Voice, out: np.ndarray) -> None:
        n = len(out)
        if v.loop:
            idx = (v.pos + np.arange(n)) % len(v.clip)
            block = v.clip[idx]
        else:
            block = np.zeros(n, np.float32)
            chunk = v.clip[v.pos:v.pos + n]
            block[:len(chunk)] = chunk
        g = np.full(n, v.gain, np.float32)
        if v.fade > 0:
            done = v.fade_len - v.fade
            g *= np.clip((done + np.arange(n)) / v.fade_len, 0, 1)
            v.fade = max(0, v.fade - n)
        if v.stop_left is not None:
            g *= np.clip((v.stop_left - np.arange(n)) / v.stop_len, 0, 1)
            v.stop_left -= n
        out += block * g
        v.pos += n
        if (v.stop_left is not None and v.stop_left <= 0) or (not v.loop and v.pos >= len(v.clip)):
            self._voices.remove(v)

    def _bed_block(self, n: int) -> np.ndarray:
        idx = (self._bed_pos + np.arange(n)) % len(self._bed)
        self._bed_pos = int((self._bed_pos + n) % len(self._bed))
        return self._bed[idx]

    def _room_target(self) -> float:
        level = ROOM_TALKING if self._speaking else ROOM_QUIET
        if self.settings.room == "low":
            level += ROOM_LOW
        return db(self._base + level)

    def _room_ramp(self, n: int) -> np.ndarray:
        target = self._room_target()
        tau = ROOM_ATTACK if target > self._room else ROOM_RELEASE
        ramp = target + (self._room - target) * np.exp(-np.arange(1, n + 1) / (tau * self.rate))
        self._room = float(ramp[-1])
        return ramp.astype(np.float32)

    def _master(self, n: int) -> np.ndarray:
        t = self.t + np.arange(n)
        g = np.clip(t / (PICKUP * self.rate), 0, 1)
        if self._ending is not None:
            g *= np.clip(1 - (t - self._ending) / (HANGUP * self.rate), 0, 1)
        return g.astype(np.float32)

    def _event_gap(self) -> int:
        mean = EVENT_GAP.get(self.settings.events, EVENT_GAP["rare"])
        return int(self.rng.expovariate(1 / mean) * self.rate) + self.t


def render_offline(scape: Soundscape, cues: Iterable[tuple[float, str, Any]], seconds: float) -> np.ndarray:
    """Run the soundscape against a script of cues ``(at_seconds, method, arg)`` — Studio's preview calls."""
    pending = sorted(cues, key=lambda c: c[0])
    out, total = [], int(seconds * scape.rate)
    while scape.t < total:
        while pending and pending[0][0] * scape.rate <= scape.t:
            _, method, arg = pending.pop(0)
            getattr(scape, method)(*(() if arg is None else (arg,)))
        out.append(scape.frame())
    return np.concatenate(out)[:total]


class SoundscapePlayer:
    """Puts a soundscape on its own track in the call's room. A short queue (100 ms) so a cut sound
    stops for the caller almost at once; LiveKit's background player buffers half a second."""

    def __init__(self, scape: Soundscape) -> None:
        self.scape = scape
        self._task: asyncio.Task | None = None
        self._source = self._track = self._room = None

    async def start(self, room: Any) -> None:
        from livekit import rtc

        self._room = room
        self._source = rtc.AudioSource(self.scape.rate, 1, queue_size_ms=100)
        self._track = rtc.LocalAudioTrack.create_audio_track("background", self._source)
        await room.local_participant.publish_track(
            self._track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        self._task = asyncio.create_task(self._run(rtc))

    async def _run(self, rtc: Any) -> None:
        n = self.scape.n
        while True:
            pcm = np.clip(self.scape.frame() * 32767, -32768, 32767).astype(np.int16)
            await self._source.capture_frame(rtc.AudioFrame(data=pcm.tobytes(), sample_rate=self.scape.rate,
                                                            num_channels=1, samples_per_channel=n))

    async def aclose(self) -> None:
        if self._task is None:
            return
        self.scape.end()
        await asyncio.sleep(HANGUP)
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        try:
            await self._room.local_participant.unpublish_track(self._track.sid)
        except Exception:  # the room is usually gone by now
            log.debug("could not unpublish the background track", exc_info=True)
