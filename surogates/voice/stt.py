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
from websockets.exceptions import ConnectionClosed

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
            sender.add_done_callback(lambda t: t.cancelled() or t.exception())  # a closed socket ends it; seen below
            speaking = False
            try:
                async for raw in ws:
                    ev = json.loads(raw)
                    if ev.get("type") == "error":
                        raise APIConnectionError(f"STT error: {(ev.get('error') or {}).get('message', '')}")
                    if ev.get("type") == "done":
                        return
                    out, speaking = translate(ev, speaking)
                    for e in out:
                        self._event_ch.send_nowait(e)
            except ConnectionClosed as e:  # the STT pod went away mid-call: LiveKit retries an APIError
                raise APIConnectionError(f"STT connection lost: {e!r}") from e
            finally:
                sender.cancel()
            # closed without "done": the caller would go unheard for the rest of the call
            raise APIConnectionError("STT closed the stream")
