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
    """An ``ask_user_question`` call as spoken questions, each with its choices read as a list.

    The tool's schema (``tools/builtin/ask_user_question.py``) is
    ``{"questions": [{"prompt": str, "choices": [{"label": str}]}]}``.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return ""
    questions = arguments.get("questions") if isinstance(arguments, dict) else None
    spoken = []
    for q in questions if isinstance(questions, list) else []:
        if not isinstance(q, dict) or not str(q.get("prompt") or "").strip():
            continue
        labels = [str(c.get("label")).strip() for c in q.get("choices") or []
                  if isinstance(c, dict) and str(c.get("label") or "").strip()]
        spoken.append(str(q["prompt"]).strip() + (f" Variante: {', '.join(labels)}." if labels else ""))
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
        """The turn's text as the agent writes it. Ends at its final answer, at a question, or when the turn ends.

        Everything before the harness starts this turn (its first ``llm.request``) belongs to the turn the
        caller just cut off: its tail, its answer and its ``session.stopped`` land after the caller's
        new words and must not be spoken or end this turn. Only a failed session ends it regardless.
        """
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"surogates:session:{self.session_id}")
        cursor, started = after, False
        try:
            while True:
                for e in await self.store.get_events(self.session_id, after=cursor):
                    cursor, data = e.id, e.data or {}
                    if e.type == EventType.SESSION_FAIL.value:
                        return
                    if not started:
                        started = e.type == EventType.LLM_REQUEST.value
                        continue
                    if e.type == EventType.LLM_DELTA.value and data.get("content"):
                        yield data["content"]
                    elif e.type == EventType.TOOL_CALL.value and data.get("name") == "ask_user_question":
                        if question := question_text(data.get("arguments")):
                            yield question
                        return
                    elif _final_answer(e) or e.type in TERMINAL:
                        return  # after the final answer only summaries and completion follow: nothing to say
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
        written = any(map(_final_answer, await self.store.get_events(self.session_id, after=self._user_event)))
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
