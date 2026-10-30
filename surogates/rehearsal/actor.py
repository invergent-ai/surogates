"""The AI that plays a rehearsal's people.

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
from surogates.rehearsal import store
from surogates.rehearsal.api import PersonMessage, write

logger = logging.getLogger(__name__)

#: How long the agent's last message must sit before the person answers: an
#: agent often sends a thought in several messages.
QUIET = timedelta(seconds=4)
#: The agent not answering for this long ends the person's part.
SILENCE = timedelta(minutes=3)
#: How many moves a person makes before giving up, and how long a run may last.
MOVE_CAP = 20
RUN_CAP = timedelta(minutes=30)
TICK_SECONDS = 2
STRIKES = 3

_TAPS = {
    "whatsapp": (
        'a reply button: {"button_reply": {"id", "title"}} from interactive.action.buttons[].reply; '
        'a list row: {"list_reply": {"id", "title"}} from interactive.action.sections[].rows[]; '
        'a template quick reply: {"quick_reply": {"text", "payload"}}'
    ),
    "telegram": 'an inline button: {"callback": {"message_id": <the message id>, "data": <its callback_data>}}',
    "slack": (
        'a button: {"action": {"ts": <the message id>, "action_id"}}; '
        'an open form (kind "view"): {"view": {"id": <its id>, "values": {block_id: answer}}}'
    ),
}


class Move(BaseModel):
    """The person's next move."""

    action: Literal["write", "tap", "done", "give_up"]
    #: What they write, for ``write``.
    text: Optional[str] = None
    #: What they tap, for ``tap``, in the shape the instructions give.
    tap: Optional[dict[str, Any]] = None
    #: For ``done`` and ``give_up``: why, in a short sentence.
    note: Optional[str] = None


def _client(settings) -> tuple[AsyncOpenAI, str] | None:
    r, llm = settings.rehearsal, settings.llm
    model = r.model or llm.summary_model
    base_url = r.base_url or llm.summary_base_url or llm.base_url
    api_key = r.api_key or llm.summary_api_key or llm.api_key
    if not (model and api_key):
        return None
    return AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=60, max_retries=1), model


def _prompt(person, channel: str, thread) -> list[dict[str, str]]:
    taps = _TAPS.get(channel)
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
        + '\nAnswer with JSON: {"action": "write"|"tap"|"done"|"give_up", "text", "tap", "note"}.'
    )
    chat = "\n".join(lines) or "(Nothing yet: you write first.)"
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"The chat so far, oldest first:\n{chat}\n\nYour next move?"}]


def _waiting(person, thread, now: datetime) -> str | None:
    """Why the person can't move yet, or ``None`` when it's their turn."""
    agent = [m for m in thread if m.direction == "out"]
    last = agent[-1].created_at if agent else None
    if person.ai_acted_at and (last is None or last <= person.ai_acted_at):
        return "no_reply" if now - person.ai_acted_at > SILENCE else "agent"
    if last and (now - last < QUIET or store.is_typing(person, now)):
        return "agent"
    return None


class Actor:
    """One loop for every AI-played person: restart-safe, as it keeps nothing but what's in flight."""

    def __init__(self, state) -> None:
        self.state = state
        self.busy: set = set()
        self.strikes: dict = {}

    async def run(self) -> None:
        while True:
            try:
                for run, person in await store.ai_people(self.state.session_factory):
                    if person.id not in self.busy:
                        self.busy.add(person.id)
                        asyncio.create_task(self._turn(run, person))
            except Exception:
                logger.exception("[rehearsal] the AI people's loop failed a pass")
            await asyncio.sleep(TICK_SECONDS)

    async def _stop(self, person, state: str, note: str) -> None:
        await store.update_person(self.state.session_factory, person.id, ai_state=state, ai_note=note)

    async def _turn(self, run, person) -> None:
        try:
            await self._move(run, person)
        except Exception as exc:
            logger.warning("[rehearsal] the AI couldn't move as %s: %s", person.id, exc)
            self.strikes[person.id] = self.strikes.get(person.id, 0) + 1
            if self.strikes[person.id] >= STRIKES:
                await self._stop(person, "failed", f"The AI couldn't continue as {person.name}")
        finally:
            self.busy.discard(person.id)

    async def _move(self, run, person) -> None:
        sf, now = self.state.session_factory, datetime.now(timezone.utc)
        if now - run.created_at > RUN_CAP:
            return await self._stop(person, "gave_up", "The rehearsal ran out of time")
        thread = await store.thread(sf, person.id)
        waiting = _waiting(person, thread, now)
        if waiting == "no_reply":
            return await self._stop(person, "no_reply", "The agent didn't answer for 3 minutes")
        if waiting:
            return
        if sum(m.direction == "in" for m in thread) >= MOVE_CAP:
            return await self._stop(person, "gave_up", f"{person.name} wrote {MOVE_CAP} times")
        client = _client(self.state.settings)
        if client is None:
            return await self._stop(person, "failed", "No model is set up to play people")
        llm, model = client
        try:
            move = await generate_structured(
                llm_client=llm, model=model, messages=_prompt(person, run.channel, thread),
                output_model=Move, max_tokens=600, temperature=0.7,
            )
        finally:
            await llm.close()
        if move is None:
            raise RuntimeError("the model gave no usable move")
        if move.action in ("done", "give_up"):
            state = "done" if move.action == "done" else "gave_up"
            return await self._stop(person, state, move.note or "")
        body = (PersonMessage(**move.tap) if move.action == "tap" and move.tap
                else PersonMessage(text=move.text))
        # Taken over while the model thought: the user writes now.
        current = next((p for p in await store.people(sf, run.id) if p.id == person.id), None)
        if current is None or current.driver != "ai":
            return
        try:
            await write(self.state, run, person, body)
        except HTTPException as exc:
            raise RuntimeError(f"the move was refused: {exc.detail}") from None
        self.strikes.pop(person.id, None)
        await store.update_person(sf, person.id, ai_acted_at=datetime.now(timezone.utc))
