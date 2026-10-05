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
