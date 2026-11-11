"""The AI that plays a simulation's people.

It reads a person's chat as the person sees it, the agent's messages in the
platform's own shape, buttons included, and writes or taps through the same
path a hand-typed message takes.  It waits for the agent between moves, and
stops when the person is done, gives up, the agent stays silent, or the
person has written too much.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel

from surogates.harness.structured_output import generate_structured
from surogates.simulation import store
from surogates.simulation.api import PersonMessage, write

logger = logging.getLogger(__name__)

#: How long the agent's last message must sit before the person answers: an
#: agent often sends a thought in several messages.
QUIET = timedelta(seconds=4)
#: The agent not answering for this long ends the person's part; while a turn
#: of its is still running, a person waits up to PATIENCE, as they would
#: watching it type.
SILENCE = timedelta(minutes=3)
PATIENCE = timedelta(minutes=10)
#: How many moves a person makes before giving up, and how long a run may last.
MOVE_CAP = 20
RUN_CAP = timedelta(minutes=30)
TICK_SECONDS = 2
STRIKES = 3

_TAPS = {
    "whatsapp": (
        'a reply button: {"button_reply": {"id", "title"}} from interactive.action.buttons[].reply; '
        'a list row: {"list_reply": {"id", "title"}} from interactive.action.sections[].rows[]; '
        'a template quick reply: {"quick_reply": {"text", "payload"}}; '
        'a form (interactive.type "flow") is not tapped: choose action "form" and put each '
        'field\'s answer in "form", by the field names listed below'
    ),
    "telegram": 'an inline button: {"callback": {"message_id": <the message id>, "data": <its callback_data>}}',
    "slack": (
        'a button: {"action": {"ts": <the message id>, "action_id"}}; '
        'an open form (kind "view"): {"view": {"id": <its id>, "values": {block_id: answer}}}'
    ),
}


class Move(BaseModel):
    """The person's next move."""

    action: Literal["write", "tap", "form", "done", "give_up"]
    #: What they write, for ``write``.
    text: Optional[str] = None
    #: What they tap, for ``tap``, in the shape the instructions give.
    tap: Optional[dict[str, Any]] = None
    #: For ``form``: each field's answer, by field name.
    form: Optional[dict[str, Any]] = None
    #: For ``done`` and ``give_up``: why, in a short sentence.
    note: Optional[str] = None


def _client(settings) -> tuple[AsyncOpenAI, str] | None:
    r, llm = settings.simulation, settings.llm
    model = r.model or llm.summary_model
    base_url = r.base_url or llm.summary_base_url or llm.base_url
    api_key = r.api_key or llm.summary_api_key or llm.api_key
    if not (model and api_key):
        return None
    return AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=60, max_retries=1), model


def _forms(flows, thread) -> str:
    """The fields of every WhatsApp Flow the agent sent, so the person can fill it in."""
    sent = {str(i.get("action", {}).get("parameters", {}).get("flow_id"))
            for i in _sent_interactive(thread)}
    lines = []
    for flow in flows or []:
        if str(flow.get("id")) not in sent:
            continue
        for screen in (flow.get("json") or {}).get("screens", []):
            for field in screen.get("layout", {}).get("children", []):
                if not field.get("name"):
                    continue
                options = [o.get("id") for o in field.get("data-source", []) if isinstance(o, dict)]
                lines.append(
                    f"- {field['name']} ({field.get('type')}, {field.get('label', '')}"
                    f"{', required' if field.get('required') else ''}"
                    f"{', one of ' + ' / '.join(map(str, options)) if options else ''})"
                )
        if lines:
            lines.insert(0, f"Form {flow.get('id')} ({flow.get('name', '')}), its fields:")
    return "\n".join(lines)


def _sent_interactive(thread) -> list[dict]:
    """The interactive part of every message the agent sent, oldest first."""
    return [m.body.get("interactive", {}) for m in thread
            if m.direction == "out" and isinstance(m.body, dict)]


def _form_token(thread) -> str | None:
    """The token of the last form the agent sent."""
    forms = [i for i in _sent_interactive(thread) if i.get("type") == "flow"]
    return forms[-1].get("action", {}).get("parameters", {}).get("flow_token") if forms else None


_A_FORM = 'that is a form, not a button: choose action "form" with each field\'s answer'


def _wrong_tap(thread, body: PersonMessage, tap: dict | None = None) -> str | None:
    """Why a WhatsApp tap matches nothing the agent sent, said so the model can fix it."""
    if tap and "flow_token" in tap and "form_reply" not in tap:
        return _A_FORM
    if body.form_reply is not None:
        filled = {k for k, v in body.form_reply.items() if k != "flow_token" and v not in (None, "")}
        return None if filled else "the form was sent empty: fill in its fields, named below"
    if not body.button_reply:
        return None
    sent = _sent_interactive(thread)
    ids = {b.get("reply", {}).get("id") for i in sent for b in i.get("action", {}).get("buttons", [])}
    if body.button_reply.get("id") in ids:
        return None
    if any(i.get("type") == "flow" for i in sent):
        return _A_FORM
    if not ids:
        return "the agent sent no buttons: write your answer as text"
    return f"no button has the id {body.button_reply.get('id')!r}; copy one from the agent's buttons"


def _prompt(person, channel: str, thread, flows=None, refused: str | None = None) -> list[dict[str, str]]:
    taps = _TAPS.get(channel)
    forms = _forms(flows, thread) if channel == "whatsapp" else ""
    lines = []
    for m in thread:
        who = "You" if m.direction == "in" else "Agent"
        body = json.dumps(m.body, ensure_ascii=False)[:1500]
        lines.append(f"[{who}] id={m.wamid} kind={m.kind} status={m.status}: {body}")
    system = (
        f"You are {person.name}, a real person writing to a business's assistant on {channel}. "
        f"Who you are and what you want:\n{person.persona}\n\n"
        "Stay in character: write the way this person writes, in their language, short like a chat "
        "message, one message per move. Never say you are an AI or that this is a test. "
        "Answer what the assistant asks when your character would. "
        "When you got what you came for, or the conversation is over, choose done. "
        "When your character would give up, choose give_up.\n"
        + (f"You can tap what the assistant offers: {taps}. Copy ids exactly from its message."
           if taps else "You can only write text.")
        + (f"\n{forms}\nAn OptIn field is true or false; a date is YYYY-MM-DD." if forms else "")
        + (f"\nYour last move was refused ({refused}); make a different one." if refused else "")
        + '\nAnswer with JSON: {"action": "write"|"tap"|"form"|"done"|"give_up", "text", "tap", "form", "note"}.'
    )
    chat = "\n".join(lines) or "(Nothing yet: you write first.)"
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"The chat so far, oldest first:\n{chat}\n\nYour next move?"}]


def _owed(person, thread) -> bool:
    """Whether the person moved and the agent hasn't written since."""
    last = max((m.created_at for m in thread if m.direction == "out"), default=None)
    return bool(person.ai_acted_at) and (last is None or last <= person.ai_acted_at)


def _waiting(person, thread, now: datetime, turn_ended_at: datetime | None = None) -> str | None:
    """Why the person can't move yet, or ``None`` when it's their turn.

    Silence counts from the end of the agent's turn on the person's move, not
    from the move: a cold worker can take minutes to pick a message up.
    """
    agent = [m for m in thread if m.direction == "out"]
    last = agent[-1].created_at if agent else None
    if _owed(person, thread):
        if turn_ended_at is None or turn_ended_at <= person.ai_acted_at:
            return "agent"
        return "no_reply" if now - turn_ended_at > SILENCE else "agent"
    if last and (now - last < QUIET or store.is_typing(person, now)):
        return "agent"
    return None


class Actor:
    """One loop for every AI-played person: restart-safe, as it keeps nothing but what's in flight."""

    def __init__(self, state) -> None:
        self.state = state
        self.busy: set = set()
        self.strikes: dict = {}
        self.refused: dict = {}

    async def run(self) -> None:
        while True:
            try:
                for run, person in await store.ai_people(self.state.session_factory):
                    if person.id not in self.busy:
                        self.busy.add(person.id)
                        asyncio.create_task(self._turn(run, person))
                await self._finish_done_runs()
            except Exception:
                logger.exception("[simulation] the AI people's loop failed a pass")
            await asyncio.sleep(TICK_SECONDS)

    async def _finish_done_runs(self) -> None:
        for run_id in await store.done_runs(self.state.session_factory):
            await store.finish_run(self.state, run_id)

    async def _stop(self, person, state: str, note: str) -> None:
        await store.update_person(self.state.session_factory, person.id, ai_state=state, ai_note=note)

    async def _turn(self, run, person) -> None:
        try:
            await self._move(run, person)
        except Exception as exc:
            logger.warning("[simulation] the AI couldn't move as %s: %s", person.id, exc)
            self.refused[person.id] = str(exc)
            self.strikes[person.id] = self.strikes.get(person.id, 0) + 1
            if self.strikes[person.id] >= STRIKES:
                # Handed back later, the person starts with a clean slate.
                self.strikes.pop(person.id, None)
                self.refused.pop(person.id, None)
                await self._stop(person, "failed", f"Autopilot couldn't continue as {person.name}")
        finally:
            self.busy.discard(person.id)

    async def _move(self, run, person) -> None:
        sf, now = self.state.session_factory, datetime.now(timezone.utc)
        thread = await store.thread(sf, person.id)
        # The team answers the agent; only one given a task starts the conversation.
        untouched = person.stands_for is not None and not person.task and not any(
            m.direction == "out" for m in thread)
        if now - run.created_at > RUN_CAP:
            if untouched:
                return await self._stop(person, "done", "The agent never wrote to them")
            return await self._stop(person, "gave_up", "The simulation ran out of time")
        if untouched:
            return
        owed = _owed(person, thread)
        ended = await store.agent_turn_ended_at(sf, person) if owed else None
        waiting = _waiting(person, thread, now, ended)
        # While the agent owes an answer the person waits, up to PATIENCE: what
        # the platform sends on its own, a disclosure or a receipt, is no answer.
        last_in = max((m.created_at for m in thread if m.direction == "in"), default=None)
        since = person.ai_acted_at or last_in or run.created_at
        if waiting == "agent":
            if owed and (ended is None or ended <= person.ai_acted_at) and now - since > PATIENCE:
                return await self._stop(person, "no_reply", "The agent didn't answer in time")
            return
        if now - since < PATIENCE and await store.agent_working(sf, person):
            return
        if waiting == "no_reply":
            if await store.left_unanswered(sf, person):
                return await self._stop(person, "done", "The agent left the last message unanswered")
            return await self._stop(person, "no_reply", "The agent didn't answer in time")
        if sum(m.direction == "in" for m in thread) >= MOVE_CAP:
            return await self._stop(person, "gave_up", f"{person.name} wrote {MOVE_CAP} times")
        client = _client(self.state.settings)
        if client is None:
            return await self._stop(person, "failed", "No model is set up to play people")
        llm, model = client
        try:
            move = await generate_structured(
                llm_client=llm, model=model, messages=_prompt(person, run.channel, thread, run.flows,
                                                         self.refused.get(person.id)),
                output_model=Move, max_tokens=600, temperature=0.7,
            )
        finally:
            await llm.close()
        if move is None:
            raise RuntimeError("the model gave no usable move")
        if move.action in ("done", "give_up"):
            state = "done" if move.action == "done" else "gave_up"
            return await self._stop(person, state, move.note or "")
        if move.action == "form":
            tap = {"form_reply": {**(move.form or {}), "flow_token": _form_token(thread)}}
        else:
            tap = move.tap if move.action == "tap" else None
        body = PersonMessage(**tap) if tap else PersonMessage(text=move.text)
        if tap and not body.model_dump(exclude_none=True):
            raise RuntimeError(f"the move was refused: {json.dumps(tap)} is not a tap; "
                               f"use {_TAPS.get(run.channel, 'text')}")
        wrong = _wrong_tap(thread, body, tap) if run.channel == "whatsapp" else None
        if wrong:
            raise RuntimeError(f"the move was refused: {wrong}")
        # Taken over while the model thought: the user writes now.
        current = next((p for p in await store.people(sf, run.id) if p.id == person.id), None)
        if current is None or current.driver != "ai":
            return
        try:
            await write(self.state, run, person, body)
        except HTTPException as exc:
            raise RuntimeError(f"the move was refused: {exc.detail}") from None
        self.strikes.pop(person.id, None)
        self.refused.pop(person.id, None)
        await store.update_person(sf, person.id, ai_acted_at=datetime.now(timezone.utc))
