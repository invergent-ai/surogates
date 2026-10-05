"""TTS speech made fit for a phone line: no padding silence, telephone level, never clipped."""
import numpy as np

from surogates.voice.audio import PhoneVoice

RATE = 24000


def _pcm(x: np.ndarray) -> bytes:
    return (x * 32767).astype("<i2").tobytes()


def _rms(pcm: bytes) -> float:
    return float(np.sqrt(np.mean(np.frombuffer(pcm, "<i2").astype(np.float64) ** 2)))


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
    assert _rms(out) > 2.5 * _rms(_pcm(quiet))
    loud = (np.sin(np.arange(RATE) * 2 * np.pi * 1000 / RATE) * 0.95).astype(np.float32)
    peak = np.abs(np.frombuffer(PhoneVoice(RATE)(_pcm(loud), last=True), "<i2"))
    assert (peak >= 32700).mean() < 0.01


def test_all_silence_yields_nothing():
    assert PhoneVoice(RATE)(b"\0\0" * RATE, last=True) == b""
