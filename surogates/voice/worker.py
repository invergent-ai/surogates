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

from livekit.agents import (
    Agent, AgentServer, AgentSession, AudioConfig, BackgroundAudioPlayer, BuiltinAudioClip, JobContext, JobProcess,
)
from livekit.plugins import silero
from redis.asyncio import Redis

from surogates.channels.resolve import resolve_tenant
from surogates.config import load_settings
from surogates.voice.agent import GOODBYE, SORRY, STILL_THERE, TURN_HANDLING, UNAVAILABLE, CallConfig, VoiceAgent
from surogates.voice.llm import SurogatesLLM
from surogates.voice.sessions import SORRY_TURN, CallTarget, VoiceSessions
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
    await session.interrupt(force=True)  # cut in: never queue a goodbye behind speech that may never end
    await session.say(text, allow_interruptions=False, add_to_chat_ctx=False).wait_for_playout()
    await ctx.delete_room()


async def apologize(ctx: JobContext, tts_url: str, voice: str, text: str) -> None:
    """Say one sentence and end the call, before (or instead of) a conversation."""
    bare = AgentSession(tts=RoTTS(url=tts_url, voice=voice))
    await bare.start(agent=Agent(instructions=""), room=ctx.room)
    await say_and_hang_up(ctx, bare, text)


async def entrypoint(ctx: JobContext) -> None:
    settings = load_settings()
    vs = settings.voice
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    info = call_info(ctx.room.name, participant.attributes)
    try:
        rt = await Runtime.open(settings)
        ctx.add_shutdown_callback(rt.aclose)
        tenant = await resolve_tenant(rt.routing, "voice", info.called) if info else None
    except Exception:  # ops down, 401, a timeout: the caller hears why, not silence
        log.exception("could not resolve room %s (called %s)", ctx.room.name, info and info.called)
        return await apologize(ctx, vs.tts_url, "female", SORRY)
    if info is None or tenant is None:
        log.warning("no agent for room %s (called %s)", ctx.room.name, info and info.called)
        return await apologize(ctx, vs.tts_url, "female", UNAVAILABLE)

    config = CallConfig.from_routing(tenant.get("config"))
    try:
        call = await rt.sessions.open_call(
            CallTarget(org_id=UUID(str(tenant["org_id"])), agent_id=tenant["agent_id"],
                       remember_callers=config.remember_callers),
            call_id=info.call_id, called=info.called, caller=info.caller, greeting=config.greeting)
    except Exception:
        log.exception("could not open a session for call %s", info.call_id)
        return await apologize(ctx, vs.tts_url, config.voice, SORRY)
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

    @session.on("error")
    def _error(ev) -> None:
        if isinstance(ev.source, SurogatesLLM):  # the turn failed: say so instead of leaving silence
            log.warning("call %s turn failed: %r", info.call_id, ev.error)
            session.say(SORRY_TURN, add_to_chat_ctx=False)

    @session.on("close")
    def _closed(ev) -> None:
        # the session gave up (unrecoverable STT/LLM/TTS errors, or the caller left): never keep a caller
        # on the line with nobody there
        log.info("call %s session closed: %s", info.call_id, ev.reason)
        spawn(ctx.delete_room())

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
                         setup_fnc=prewarm, num_idle_processes=vs.idle_processes,
                         initialize_process_timeout=vs.process_init_timeout, port=vs.health_port,
                         load_threshold=1.0,
                         load_fnc=lambda s: len(s.active_jobs) / max(vs.max_calls, 1))
    server.rtc_session(entrypoint, agent_name=vs.agent_name)
    await server.run()
