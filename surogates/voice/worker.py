"""``surogates voice``: answers phone calls (LiveKit SIP) with the agent their number belongs to.

One LiveKit job per call, each in its own process. A job reads which of our numbers was called,
resolves it through the ops channel routing (``voice:+40…``), opens the call's session and runs
the conversation until someone hangs up.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import numpy as np
from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess
from livekit.plugins import silero
from redis.asyncio import Redis

from surogates.channels.resolve import resolve_tenant
from surogates.config import load_settings
from surogates.voice.agent import CallConfig, VoiceAgent
from surogates.voice.capacity import CallSlots
from surogates.voice.llm import SurogatesLLM
from surogates.voice.soundscape import Pack, Soundscape, SoundscapePlayer, fetch_pack
from surogates.voice.sessions import CallTarget, VoiceSessions, normalize_caller
from surogates.voice.text import asks_for_details
from surogates.voice.speech import Slot, build_stt, build_tts, turn_handling
from surogates.voice.tts import PhraseCache, RoTTS

log = logging.getLogger("surogates.voice")
NO_LINE = CallConfig().lines  # before a number's agent is known, its lines are too: the platform default


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


class ProviderUnavailable(Exception):
    """A line's speech provider cannot be used for this call (its key is missing or unreadable)."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"{provider}: {reason}")
        self.provider, self.reason = provider, reason


@dataclass
class Runtime:
    """What one call needs from the platform: DB, Redis, ops routing. Built per job process."""

    engine: Any
    redis: Any
    client: Any
    routing: Any
    sessions: VoiceSessions
    vault: Any = None  # owners' provider keys; None when the platform has no encryption key

    @classmethod
    async def open(cls, settings: Any) -> Runtime:
        from surogates.api.app import _build_vault, build_channel_routing_cache
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
        return cls(engine=engine, redis=redis, client=client, routing=routing, sessions=sessions,
                   vault=_build_vault(settings.encryption_key, sf))

    async def key(self, org_id: UUID, slot: Slot) -> str:
        """The owner's key for a provider slot, from the vault. Raises ProviderUnavailable: a provider
        line without its key cannot speak, and the caller must hear that instead of silence."""
        if slot.ours or not slot.key_ref:
            return ""
        from surogates.tenant.credentials import parse_vault_ref

        if self.vault is None:
            raise ProviderUnavailable(slot.provider, "no_vault")
        try:
            value = await self.vault.retrieve(org_id, parse_vault_ref(slot.key_ref))
        except Exception as e:  # a bad ref, an undecryptable value, the database
            raise ProviderUnavailable(slot.provider, "key_unreadable") from e
        if not value:
            raise ProviderUnavailable(slot.provider, "key_missing")
        return value

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


def prewarm(proc: JobProcess) -> None:
    """Runs in each warm process before it gets a call: what loads here is not paid during a greeting."""
    proc.userdata["vad"] = silero.VAD.load()
    # the first call in a process imported these mid-greeting (the database, storage and runtime stack)
    import surogates.api.app  # noqa: F401
    import surogates.db.engine  # noqa: F401
    import surogates.runtime.platform_client  # noqa: F401
    import surogates.session.store  # noqa: F401
    import surogates.storage.backend  # noqa: F401


def fixed(session: AgentSession, phrases: PhraseCache | None, text: str, **kw):
    """Say a fixed phrase, from the phrase cache when there is one (instant after its first use)."""
    return session.say(text, audio=phrases.frames(text), **kw) if phrases else session.say(text, **kw)


async def say_and_hang_up(ctx: JobContext, session: AgentSession, text: str,
                          phrases: PhraseCache | None = None) -> None:
    await session.interrupt(force=True)  # cut in: never queue a goodbye behind speech that may never end
    await fixed(session, phrases, text, allow_interruptions=False, add_to_chat_ctx=False).wait_for_playout()
    await ctx.delete_room()


async def apologize(ctx: JobContext, tts: Any, text: str, phrases: PhraseCache | None = None) -> None:
    """Say one sentence and end the call, before (or instead of) a conversation."""
    bare = AgentSession(tts=tts)
    await bare.start(agent=Agent(instructions=""), room=ctx.room)
    await say_and_hang_up(ctx, bare, text, phrases)


def tone(rate: int = 16000) -> list[rtc.AudioFrame]:
    """Two short low beeps: the line's own voice could not be reached, and no other voice may pretend
    to be it (or speak a language the caller never chose)."""
    t = np.arange(int(0.18 * rate)) / rate
    beep = (0.25 * np.sin(2 * np.pi * 480 * t) * np.minimum(1, np.minimum(t, t[::-1]) / 0.01) * 32767).astype("<i2")
    gap = np.zeros(int(0.12 * rate), "<i2")
    pcm = np.concatenate([beep, gap, beep, np.zeros(int(0.3 * rate), "<i2")]).tobytes()
    return [rtc.AudioFrame(data=pcm, sample_rate=rate, num_channels=1, samples_per_channel=len(pcm) // 2)]


async def tone_and_hang_up(ctx: JobContext, tts_url: str) -> None:
    bare = AgentSession(tts=RoTTS(url=tts_url))  # never synthesizes: the tone is the audio

    async def frames():
        for f in tone():
            yield f

    await bare.start(agent=Agent(instructions=""), room=ctx.room)
    await bare.say(" ", audio=frames(), allow_interruptions=False, add_to_chat_ctx=False).wait_for_playout()
    await ctx.delete_room()


async def run_all(*steps: tuple[str, Any]) -> None:
    """Run each ``(name, awaitable)`` in order; a step that fails is logged and the next one still runs.
    A ``None`` step (nothing to close) is skipped."""
    for name, step in steps:
        if step is None:
            continue
        try:
            await step
        except Exception:
            log.warning("call cleanup: %s failed", name, exc_info=True)


def follow_call(session: AgentSession, agent: Any, scape: Any, call: Any) -> None:
    """The background follows the call: who speaks, the agent thinking or looking something up, the
    caller giving details."""
    call.on_lookup = scape.lookup
    pen_for = None  # the question the pen last wrote for

    @session.on("agent_state_changed")
    def _agent(ev) -> None:
        scape.agent_speaking(ev.new_state == "speaking")
        scape.agent_thinking(ev.new_state == "thinking")

    @session.on("user_state_changed")
    def _caller(ev) -> None:
        nonlocal pen_for
        speaking = ev.new_state == "speaking"
        if speaking and agent.last_said != pen_for:
            pen_for = agent.last_said  # once per question, however many breaths the answer takes
            if asks_for_details(agent.last_said, agent.config.language):
                scape.writing()
        scape.caller_speaking(speaking)


async def start_background(ctx: JobContext, session: AgentSession, agent: Any, call: Any, config: CallConfig,
                           client: Any, cache: str) -> SoundscapePlayer | None:
    """The call's background sound, if the agent has one. Never fails the call: no pack, no background."""
    if config.sound.silent:
        return None
    try:
        directory = await fetch_pack(client.get_voice_sound, Path(cache).expanduser(), config.sound)
        if directory is None:
            return None
        pack = await asyncio.to_thread(Pack.load, directory, config.sound)
        scape = Soundscape(pack, config.sound)
        follow_call(session, agent, scape, call)
        player = SoundscapePlayer(scape)
        await player.start(ctx.room)
        return player
    except Exception:
        log.warning("call %s: no background sound", ctx.room.name, exc_info=True)
        return None


async def entrypoint(ctx: JobContext) -> None:
    settings = load_settings()
    vs = settings.voice
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    info = call_info(ctx.room.name, participant.attributes)
    call, tasks, tts, heard, slots, tenant, background = None, set(), None, None, None, None, None
    started = datetime.now(timezone.utc)

    async def report(outcome: str) -> None:
        """Tell ops about this call (recorded now, charged later). Never raises."""
        ended = datetime.now(timezone.utc)
        await rt.client.report_voice_call(
            call_id=info.call_id, agent_id=tenant["agent_id"], number=info.called,
            caller=call.caller if call is not None else normalize_caller(info.caller),
            session_id=str(call.session_id) if call is not None else None,
            started_at=started.isoformat(), ended_at=ended.isoformat(),
            seconds=int((ended - started).total_seconds()), outcome=outcome)

    async def cleanup() -> None:
        # what the platform needs first (the session closed, the call reported, the line freed), while
        # Redis and the DB are still open; then the media. Each step runs even if an earlier one fails:
        # a stuck background track once kept every line taken.
        for task in list(tasks):
            task.cancel()
        await run_all(
            ("end the session", call and call.end()),
            ("report the call", call and report("completed")),
            ("release the line", slots and slots.release(info.call_id)),
            ("stop the background", background and background.aclose()),
            ("close the TTS", tts and tts.aclose()),
            ("close the STT", heard and heard.aclose()),
            ("close the runtime", rt.aclose()),
        )

    try:
        rt = await Runtime.open(settings)
        ctx.add_shutdown_callback(cleanup)
        tenant = await resolve_tenant(rt.routing, "voice", info.called) if info else None
    except Exception:  # ops down, 401, a timeout: the caller hears why, not silence
        log.exception("could not resolve room %s (called %s)", ctx.room.name, info and info.called)
        return await apologize(ctx, RoTTS(url=vs.tts_url), NO_LINE.sorry)
    if info is None or tenant is None:
        log.warning("no agent for room %s (called %s)", ctx.room.name, info and info.called)
        default = RoTTS(url=vs.tts_url)
        return await apologize(ctx, default, NO_LINE.unavailable, PhraseCache(rt.redis, default))

    config = CallConfig.from_routing(tenant.get("config"))
    org_id = UUID(str(tenant["org_id"]))
    try:  # the line's own voice and ears; a provider line without its key cannot take the call
        tts = build_tts(config.speaking, key=await rt.key(org_id, config.speaking), language=config.language,
                        tts_url=vs.tts_url)
        heard = build_stt(config.hearing, key=await rt.key(org_id, config.hearing), language=config.language,
                          stt_url=vs.stt_url)
    except Exception as e:
        log.warning("call %s refused: %s", info.call_id, e)
        await report("provider_error")
        return await tone_and_hang_up(ctx, vs.tts_url)
    phrases = PhraseCache(rt.redis, tts, scope="" if config.speaking.ours else str(org_id))
    slots = CallSlots(rt.redis, vs.max_concurrent_calls)  # releasing a line never taken is a no-op
    if not await slots.take(info.call_id, hold_seconds=config.max_call_seconds + 60):
        log.warning("call %s refused: all %d lines busy", info.call_id, vs.max_concurrent_calls)
        await report("busy")
        return await apologize(ctx, tts, config.lines.busy, phrases)
    try:
        call = await rt.sessions.open_call(
            CallTarget(org_id=org_id, agent_id=tenant["agent_id"],
                       remember_callers=config.remember_callers),
            call_id=info.call_id, called=info.called, caller=info.caller, greeting=config.lines.greeting,
            language=config.language, lines=config.lines)
    except Exception:
        log.exception("could not open a session for call %s", info.call_id)
        await report("error")
        return await apologize(ctx, tts, config.lines.sorry, phrases)
    log.info("call %s to %s from %s -> agent %s session %s", info.call_id, info.called, call.caller,
             tenant["agent_id"], call.session_id)
    try:  # how the call reads in Studio's session list; cosmetic, never worth failing a call over
        await call.store.update_session_title_if_empty(call.session_id, f"Call from {call.caller}")
    except Exception:
        log.debug("could not title call %s", info.call_id, exc_info=True)

    agent = VoiceAgent(config)
    session = AgentSession(vad=ctx.proc.userdata["vad"], stt=heard, llm=SurogatesLLM(call),
                           tts=tts, turn_handling=turn_handling(config.hearing),
                           user_away_timeout=config.idle_ask_seconds)

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @session.on("error")
    def _error(ev) -> None:
        if isinstance(ev.source, SurogatesLLM):  # the turn failed: say so instead of leaving silence
            log.warning("call %s turn failed: %r", info.call_id, ev.error)
            session.say(config.lines.sorry_turn, add_to_chat_ctx=False)

    @session.on("close")
    def _closed(ev) -> None:
        # the session gave up (unrecoverable STT/LLM/TTS errors, or the caller left): never keep a caller
        # on the line with nobody there
        log.info("call %s session closed: %s", info.call_id, ev.reason)
        spawn(hang_up())

    @session.on("agent_state_changed")
    def _done_speaking(ev) -> None:
        if ev.new_state == "listening" and (agent.hangup_after_reply or call.ending):
            agent.hangup_after_reply = False
            spawn(hang_up())

    async def hang_up() -> None:
        await ctx.delete_room()  # a Future, not a coroutine: spawn() needs this wrapper

    async def still_there() -> None:
        await fixed(session, phrases, config.lines.still_there, add_to_chat_ctx=False).wait_for_playout()
        await asyncio.sleep(config.idle_hangup_seconds)
        if session.user_state == "away":
            await say_and_hang_up(ctx, session, config.lines.goodbye, phrases)

    @session.on("user_state_changed")
    def _away(ev) -> None:
        if ev.new_state == "away" and session.agent_state == "listening":
            spawn(still_there())

    async def time_limit() -> None:
        await asyncio.sleep(config.max_call_seconds)
        await say_and_hang_up(ctx, session, config.lines.goodbye, phrases)

    await session.start(agent=agent, room=ctx.room)
    fixed(session, phrases, config.lines.greeting)

    async def _background() -> None:  # alongside the greeting: a slow download never delays the call
        nonlocal background
        background = await start_background(ctx, session, agent, call, config, rt.client, vs.sounds_cache)

    spawn(_background())
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
