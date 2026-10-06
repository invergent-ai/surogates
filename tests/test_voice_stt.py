"""Our Romanian STT's events become LiveKit's: final before end-of-speech, no ghost turns from idle finals."""
import json

import pytest
from livekit import rtc
from livekit.agents import APIConnectOptions, APIError, stt
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
        stream = RoSTT(url=f"ws://127.0.0.1:{port}").stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_frame(_frame())
        with pytest.raises(APIError):
            async for _ in stream:
                pass


async def _stream_until_error(handler):
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stream = RoSTT(url=f"ws://127.0.0.1:{port}").stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_frame(_frame())
        with pytest.raises(APIError):
            async for _ in stream:
                pass


async def test_a_dropped_stt_connection_is_a_retryable_error():
    """An STT pod restarting mid-call must surface as an API error LiveKit retries, not a dead ear."""
    async def handler(ws):
        await ws.send(json.dumps({"type": "ready"}))
        await ws.recv()
        ws.transport.abort()  # no close frame: the pod went away

    await _stream_until_error(handler)


async def test_a_clean_close_before_done_is_an_error_too():
    async def handler(ws):
        await ws.send(json.dumps({"type": "ready"}))
        await ws.recv()
        await ws.close()

    await _stream_until_error(handler)
