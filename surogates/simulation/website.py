"""Autopilot as a website visitor.

It opens a widget session and writes through the public routes the widget
calls, so the agent meets it as it meets anyone on the site: origin check,
message cap and all.  Those routes count against the business's per-tenant
rate limit too, so a busy simulation shares it with the real site's visitors.
The agent's replies and questions are read from the session's events, the
ones the widget shows, straight from the runtime DB: a poll every tick is
cheaper than an SSE stream.

Every website message's ``wamid`` is ``web:<event id>``: a unique index on
those makes logging them idempotent when the actor and Studio collect at once.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql, sqlite

from surogates.channels.website_session import COOKIE_NAME, CSRF_HEADER_NAME
from surogates.db.models import Event, SimulationMessage
from surogates.simulation import store

ASK = "ask_user_question"
#: What closes a question: its answer, or the call ending without one (timeout, dismissal).
_CLOSED = ("tool.result", "ask_user_question.response")
PREFIX = "web:"


def visitor_session(person) -> tuple[str, str, str] | None:
    """``(session id, csrf token, session cookie)`` once the visitor first wrote.

    The address holds them space-separated, as the widget's browser would hold
    them; a visitor who hasn't written yet is ``"visitor"``.
    """
    parts = (getattr(person, "address", "") or "").split()
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else None


def _api(state, run) -> str:
    # Ops doesn't keep the public URL on the run; the runtime's own API serves the same routes.
    return (run.routing_config.get("website_api_url") or state.settings.worker.api_base_url).rstrip("/")


def _origin(run) -> str:
    return (run.routing_config.get("allowed_origins") or [""])[0]


async def _open(state, run, person, *, fresh: bool = False) -> tuple[str, str, str] | int:
    """The visitor's widget session, opened now if they have none (or *fresh*); the status when it can't be."""
    session = None if fresh else visitor_session(person)
    if session is not None:
        return session
    r = await state.http.post(f"{_api(state, run)}/v1/website/sessions", headers={
        "Authorization": f"Bearer {run.phone_number_id}", "Origin": _origin(run)})
    # Read by hand: the cookie is Secure, and the runtime API is often reached over plain http.
    cookie = next((h.split(";", 1)[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie")
                   if h.startswith(f"{COOKIE_NAME}=")), None)
    if r.status_code not in (200, 201) or cookie is None:
        return r.status_code
    session = (r.json()["session_id"], r.json()["csrf_token"], cookie)
    person.address = " ".join(session)
    await store.update_person(state.session_factory, person.id, address=person.address)
    return session


async def _post(state, run, person, path: str, body: dict) -> Any:
    """A cookie-authenticated widget call; an expired cookie is renewed once."""
    for fresh in (False, True):
        session = await _open(state, run, person, fresh=fresh)
        if isinstance(session, int):
            return session
        r = await state.http.post(
            f"{_api(state, run)}/v1/website/sessions/{session[0]}/{path}",
            json=body, headers={"Origin": _origin(run), CSRF_HEADER_NAME: session[1],
                                "Cookie": f"{COOKIE_NAME}={session[2]}"})
        if r.status_code != 401:
            return r
    raise HTTPException(status_code=502, detail="The visitor's widget session expired and could not be renewed")


def _arguments(data: dict) -> dict:
    args = data.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    return args if isinstance(args, dict) else {}


def _question(data: dict) -> dict[str, Any]:
    """An ``ask_user_question`` call as the visitor sees it in the widget."""
    args = _arguments(data)
    body: dict[str, Any] = {"tool_call_id": data.get("tool_call_id"), "questions": [
        {"prompt": q.get("prompt"),
         "choices": [c.get("label") for c in q.get("choices") or [] if isinstance(c, dict)],
         "allow_other": q.get("allow_other", True) is not False}
        for q in args.get("questions") or [] if isinstance(q, dict) and q.get("prompt")]}
    # A reply that ends in a question may carry its only words here.
    for key in ("message", "context"):
        if isinstance(args.get(key), str) and args[key].strip():
            body[key] = args[key]
    return body


async def _closed(db, session_id: uuid.UUID) -> set[str]:
    return set((await db.execute(
        sa.select(Event.data["tool_call_id"].as_string())
        .where(Event.session_id == session_id, Event.type.in_(_CLOSED))
    )).scalars().all())


async def _open_question(sf, session) -> dict | None:
    session_id = uuid.UUID(session[0])
    async with sf() as db:
        asked = (await db.execute(
            sa.select(Event.data)
            .where(Event.session_id == session_id, Event.type == "tool.call",
                   Event.data["name"].as_string() == ASK)
            .order_by(Event.id)
        )).scalars().all()
        closed = await _closed(db, session_id)
    waiting = [q for q in map(_question, asked) if q["questions"] and q["tool_call_id"] not in closed]
    return waiting[-1] if waiting else None


async def _log(sf, run, person, *, event_id: int, direction: str, kind: str, body: dict,
               status: str) -> None:
    """One website message, once: a second log of the same event is dropped."""
    async with sf() as db:
        dialect = postgresql if (await db.connection()).dialect.name == "postgresql" else sqlite
        await db.execute(
            dialect.insert(SimulationMessage)
            .values(run_id=run.id, person_id=person.id, direction=direction, kind=kind, body=body,
                    wamid=f"{PREFIX}{event_id}", status=status)
            .on_conflict_do_nothing(index_elements=["run_id", "wamid"],
                                    index_where=SimulationMessage.wamid.like(f"{PREFIX}%")))
        await db.commit()


async def send(state, run, person, text: str) -> dict[str, Any]:
    """The visitor's message, posted as the widget posts it; an open question takes it as the answer."""
    sf = state.session_factory
    await collect(state, run, person)
    session = visitor_session(person)
    question = await _open_question(sf, session) if session else None
    if question is not None:
        [only, *more] = question["questions"]
        if more or not only["allow_other"]:
            raise HTTPException(status_code=422, detail=(
                "the agent is waiting on its questions: answer them with a tap, "
                '{"answer": {"tool_call_id", "responses": [{"question", "answer"}]}}'))
        return await answer(state, run, person, question["tool_call_id"],
                            [{"question": only["prompt"], "answer": text}])
    r = await _post(state, run, person, "messages", {"content": text})
    if isinstance(r, int):
        return {"delivered": False, "channels_status": r}
    if r.status_code != 202:
        raise HTTPException(status_code=422, detail=f"the message was refused: {r.text[:300]}")
    await _log(sf, run, person, event_id=r.json()["event_id"], direction="in", kind="text",
               body={"text": text}, status="delivered")
    return {"delivered": True, "channels_status": 202}


async def answer(state, run, person, tool_call_id: str, responses: list[dict]) -> dict[str, Any]:
    """The visitor's answers to the agent's questions, submitted as the widget submits them."""
    sf = state.session_factory
    await collect(state, run, person)
    r = await _post(state, run, person, f"ask_user_question/{tool_call_id}/respond",
                    {"responses": responses})
    if isinstance(r, int):
        return {"delivered": False, "channels_status": r}
    if r.status_code == 409:
        # Answered already, or the call stopped waiting: nothing to repeat, nothing wrong.
        await _close(sf, run, person, tool_call_id)
        return {"delivered": False, "channels_status": 409, "already_answered": True}
    if r.status_code != 201:
        raise HTTPException(status_code=422, detail=f"the answer was refused: {r.text[:300]}")
    await _log(sf, run, person, event_id=r.json()["event_id"], direction="in", kind="answer",
               body={"text": "\n".join(f"{x['question']}: {x['answer']}" for x in responses),
                     "answer": {"tool_call_id": tool_call_id, "responses": responses}},
               status="delivered")
    await _close(sf, run, person, tool_call_id)
    return {"delivered": True, "channels_status": 201}


def _open_questions(person_id):
    return (sa.select(SimulationMessage)
            .where(SimulationMessage.person_id == person_id, SimulationMessage.kind == "question",
                   SimulationMessage.status != "answered"))


async def _close(sf, run, person, tool_call_id: str) -> None:
    async with sf() as db:
        for m in (await db.execute(_open_questions(person.id))).scalars():
            if m.body.get("tool_call_id") == tool_call_id:
                m.status = "answered"
        await db.commit()


async def collect(state, run, person) -> None:
    """Log the agent's replies and questions to the visitor that came since the last one logged."""
    session = visitor_session(person)
    if session is None:
        return
    sf, session_id = state.session_factory, uuid.UUID(session[0])
    async with sf() as db:
        last = (await db.execute(
            sa.select(SimulationMessage.wamid)
            .where(SimulationMessage.person_id == person.id, SimulationMessage.direction == "out",
                   SimulationMessage.wamid.like(f"{PREFIX}%"))
            .order_by(SimulationMessage.id.desc()).limit(1)
        )).scalar_one_or_none()
        after = int(last[len(PREFIX):]) if last else 0
        said = (await db.execute(
            sa.select(Event.id, Event.type, Event.data)
            .where(Event.session_id == session_id, Event.id > after,
                   (Event.type == "llm.response")
                   | ((Event.type == "tool.call") & (Event.data["name"].as_string() == ASK)))
            .order_by(Event.id)
        )).all()
        closed = await _closed(db, session_id)
        waiting = (await db.execute(_open_questions(person.id))).scalars().all()
        for m in waiting:
            if m.body.get("tool_call_id") in closed:
                m.status = "answered"
        await db.commit()
    for event_id, kind, data in said:
        if kind == "tool.call":
            body = _question(data)
            if body["questions"]:
                await _log(sf, run, person, event_id=event_id, direction="out", kind="question",
                           body=body, status="answered" if body["tool_call_id"] in closed else "")
            continue
        content = (data.get("message") or {}).get("content") or data.get("content")
        if isinstance(content, str) and content.strip():
            await _log(sf, run, person, event_id=event_id, direction="out", kind="text",
                       body={"text": content}, status="delivered")
