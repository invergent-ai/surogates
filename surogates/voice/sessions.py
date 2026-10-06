"""A phone call's agent session, driven through the runtime's internals rather than the HTTP API.

One session per call (``channel="voice"``). Each caller utterance is a ``user.message`` plus a
wake on the shared queue; the answer is read back from the event log as it is written, nudged by
the session's pub/sub channel (the nudge carries only an id, so events are re-read).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from surogates.channels.identity import get_or_create_channel_identity, get_or_create_channel_session
from surogates.channels.inbound import build_principal_stamp
from surogates.config import INTERRUPT_CHANNEL_PREFIX, SHARED_WORK_QUEUE_KEY, encode_queue_member
from surogates.session.events import EventType
from surogates.session.interactive_input import try_resolve_text_answer
from surogates.tools.builtin.ask_user_question import ASK_USER_QUESTION_MAX_WAIT_SECONDS

log = logging.getLogger("surogates.voice")

TERMINAL = frozenset({"session.complete", "session.fail", "session.stopped", "session.pause"})
VOICE_PRIORITY = -1.0  # work-queue score: lower pops first; everything else is enqueued at 0
POLL_SECONDS = 0.4  # pub/sub is a nudge; poll as a fallback, like the OpenAI route
START_TIMEOUT = 45.0  # seconds for the harness to pick the turn up (a backlogged or dead worker)
TURN_TIMEOUT = 120.0  # seconds for the whole turn, tools included
class TurnFailed(Exception):
    """The agent's turn failed before it said anything (``session.fail``): the caller must hear an apology."""


FILLER = "O clipă, verific."  # said for the agent when it starts a tool without a word
SORRY_TURN = "Îmi pare rău, nu am reușit să răspund acum. Vă rog să mai întrebați o dată."
ANONYMOUS = "anonymous"
HEARD_NONE = "[Apelantul te-a întrerupt înainte să audă răspunsul tău anterior.] "
HEARD_PART = "[Apelantul te-a întrerupt; din răspunsul tău anterior a auzit doar: «{}».] "
GREETED = "[Ai răspuns deja la telefon cu: «{}».] "
GREETING_CUT = "[Ai răspuns la telefon, dar apelantul te-a întrerupt după: «{}».] "


def normalize_caller(raw: str | None) -> str:
    """The caller's number as identities store it (digits only, no '+'), or ``anonymous`` when withheld.

    One caller is one number however the carrier writes it: spaces and punctuation go, and a
    Romanian national number (0721…) gets its country code (40721…).
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10 and digits.startswith("0"):
        digits = "4" + digits
    return digits if 3 <= len(digits) <= 15 else ANONYMOUS


def _words(text: str) -> str:
    """Lowercase words without diacritics or punctuation, for "was this already said"."""
    text = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    return " ".join(re.findall(r"\w+", text))


def question_text(arguments: Any, said: str = "") -> str:
    """An ``ask_user_question`` call as spoken questions, each with its choices read as a list.

    A prompt the agent already spoke in ``said`` is not repeated: only its choices are added. Models
    write the question and then call the tool with it, rarely in the same words, so when what they
    just said ends in a question, the first prompt counts as said.

    The tool's schema (``tools/builtin/ask_user_question.py``) is
    ``{"questions": [{"prompt": str, "choices": [{"label": str}]}]}``.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return ""
    questions = arguments.get("questions") if isinstance(arguments, dict) else None
    spoken, asked = [], said.rstrip().endswith("?")
    for q in questions if isinstance(questions, list) else []:
        if not isinstance(q, dict) or not str(q.get("prompt") or "").strip():
            continue
        labels = [str(c.get("label")).strip() for c in q.get("choices") or []
                  if isinstance(c, dict) and str(c.get("label") or "").strip()]
        choices = f"Variante: {', '.join(labels)}." if labels else ""
        repeated = (asked and not spoken) or _words(str(q["prompt"])) in _words(said)
        prompt = "" if repeated else str(q["prompt"]).strip()
        spoken.append(" ".join(p for p in (prompt, choices) if p))
    return " ".join(spoken)


def _final_answer(e: Any) -> bool:
    """An ``llm.response`` that concludes the turn (one carrying tool calls is on its way to a tool)."""
    return e.type == EventType.LLM_RESPONSE.value and not ((e.data or {}).get("message") or {}).get("tool_calls")


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
    ending: bool = False  # the agent called end_call: hang up once its goodbye is spoken
    _user_event: int = 0

    async def send(self, text: str) -> int:
        """Post what the caller said and wake the agent; returns the event id to stream after."""
        try:  # the API route's safeguards: a question near its deadline counts as expired; a failure is a message
            answered = await try_resolve_text_answer(self.store, session_id=self.session_id, text=text,
                                                     max_age_seconds=ASK_USER_QUESTION_MAX_WAIT_SECONDS - 60)
        except Exception:
            log.warning("pending-question resolution failed for %s; sending as a message", self.session_id, exc_info=True)
            answered = None
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
        # a caller is waiting on the line: ahead of ordinary work (score 0), and LT so a later plain
        # re-enqueue of the same member can never push it back
        member = encode_queue_member(org_id=str(self.org_id), agent_id=self.agent_id, session_id=str(self.session_id))
        await self.redis.zadd(SHARED_WORK_QUEUE_KEY, {member: VOICE_PRIORITY}, lt=True)
        return self._user_event

    async def stream(self, after: int, *, start_timeout: float = START_TIMEOUT,
                     turn_timeout: float = TURN_TIMEOUT) -> AsyncIterator[str]:
        """The turn's text as the agent writes it. Ends at its final answer, at a question, or when the turn ends.

        Everything before the harness starts this turn (its first ``llm.request``) belongs to the turn the
        caller just cut off: its tail, its answer and its ``session.stopped`` land after the caller's
        new words and must not be spoken or end this turn. Only a failed session ends it regardless.
        A turn that does not start, or does not finish, in time ends with an apology and is stopped:
        a caller must never be left listening to silence.
        """
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"surogates:session:{self.session_id}")
        loop = asyncio.get_running_loop()
        began = loop.time()
        cursor, started, said, announced = after, False, "", False
        try:
            while True:
                for e in await self.store.get_events(self.session_id, after=cursor):
                    cursor, data = e.id, e.data or {}
                    if e.type == EventType.SESSION_FAIL.value:
                        if said:  # the caller already has an answer, cut short
                            return
                        raise TurnFailed(str(data.get("reason") or "session failed"))
                    if not started:
                        started = e.type == EventType.LLM_REQUEST.value
                        continue
                    if e.type == EventType.LLM_DELTA.value and data.get("content"):
                        said += data["content"]
                        yield data["content"]
                    elif e.type == EventType.TOOL_CALL.value and data.get("name") == "ask_user_question":
                        if question := question_text(data.get("arguments"), said):
                            yield f" {question}" if said else question
                        return
                    elif e.type == EventType.TOOL_CALL.value and data.get("name") == "end_call":
                        self.ending = True  # its goodbye follows; nothing to announce
                    elif e.type == EventType.TOOL_CALL.value and not said and not announced:
                        # a tool started in silence: the caller would hear only typing until it returns
                        announced = True
                        yield FILLER + " "  # the space releases it from the sentence splitter now
                    elif _final_answer(e):
                        # an answer written without deltas (non-streaming fallback, budget summary) is still the answer
                        if not said and (content := str((data.get("message") or {}).get("content") or "").strip()):
                            yield content
                        return  # after the final answer only summaries and completion follow: nothing to say
                    elif e.type in TERMINAL:
                        return
                if loop.time() - began > (turn_timeout if started else start_timeout):
                    await self.interrupt()
                    yield SORRY_TURN
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

    async def end(self) -> None:
        """The caller hung up: stop any turn still running and close the session.

        A voice session left ``active`` looks abandoned to the orphan sweeper, which re-runs its last
        turn (tools, browsers, model calls) for a caller who is gone, and fails a silent call after
        repeated recoveries. ``completed`` with ``call_ended`` is the end of the conversation.
        """
        # always: it stops a turn still running and tears the call's browser down (dispatcher)
        await self.redis.publish(f"{INTERRUPT_CHANNEL_PREFIX}:{self.session_id}", json.dumps({"reason": "call_ended"}))
        status = (await self.store.get_session(self.session_id)).status
        if status != "completed":  # a turn that already completed the session leaves nothing to close
            await self.store.update_session_status(self.session_id, "completed")
            await self.store.emit_event(self.session_id, EventType.SESSION_COMPLETE, {"reason": "call_ended"})

    async def record_heard(self, heard: str) -> None:
        """Make the history say what the caller actually heard of the answer they cut off.

        Stopped mid-generation, the harness persists no reply, so the heard part becomes the reply.
        If any reply of this turn was already written (the final answer, or the preamble that called
        a tool), the agent is told on the caller's next words instead. A cut greeting is not in the
        session at all: the note about it says how far it got.
        """
        heard = heard.strip()
        if not self._user_event:  # nothing asked yet: it was the greeting
            self.note = GREETING_CUT.format(heard) if heard else ""
            return
        events = await self.store.get_events(self.session_id, after=self._user_event)
        written = any(e.type == EventType.LLM_RESPONSE.value for e in events)
        # ponytail: a reply persisted between this read and the stop lands whole beside the heard part;
        # the agent then sees both. Rare (the stop and the persist must cross within one read).
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
        """A fresh session for this call. It is the call's own, identity and memory both, unless the agent
        remembers callers and the number is known: then the number is the identity and the memory scope.

        The identity matters as much as the memory boundary: session_search and other per-user lookups
        are scoped by user, and a caller ID can be spoofed, so a shared identity would let one caller
        reach another's past calls (and every withheld number would be the same user).
        """
        caller_id = normalize_caller(caller)
        remember = target.remember_callers and caller_id != ANONYMOUS
        ident = await get_or_create_channel_identity(
            self._sf, platform="voice", platform_user_id=caller_id if remember else f"call:{call_id}",
            org_id=target.org_id, display_name=caller_id if caller_id != ANONYMOUS else "apelant anonim")
        session_id = await get_or_create_channel_session(
            self._store, self._redis, session_key=f"agent:voice:call:{call_id}", user_id=ident.user_id,
            org_id=target.org_id, agent_id=target.agent_id, channel="voice",
            config={"memory_boundary": f"phone:{caller_id}" if remember else f"voice:call:{call_id}",
                    "voice_call_id": call_id, "voice_called": called, "voice_caller": caller_id, "multi_party": False},
            session_factory=self._sf, storage=self._storage, settings=self._settings)
        return CallSession(store=self._store, redis=self._redis, session_id=session_id, org_id=target.org_id,
                           agent_id=target.agent_id, user_id=ident.user_id, caller=caller_id,
                           note=GREETED.format(greeting) if greeting else "")
