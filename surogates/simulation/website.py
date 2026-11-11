"""Autopilot as a website visitor.

It opens a widget session and writes through the public routes the widget
calls, so the agent meets it as it meets anyone on the site: origin check,
rate limit, message cap and all.  The agent's replies and questions are read
from the session's events, the ones the widget shows, straight from the
runtime DB: a poll every tick is cheaper than an SSE stream.
"""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException

from surogates.channels.website_session import COOKIE_NAME, CSRF_HEADER_NAME
from surogates.db.models import Event
from surogates.simulation import store

ASK = "ask_user_question"
#: What closes a question: its answer, or the call ending without one (timeout, dismissal).
_CLOSED = ("tool.result", "ask_user_question.response")


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


def _headers(run, session) -> dict[str, str]:
    return {"Origin": _origin(run), CSRF_HEADER_NAME: session[1], "Cookie": f"{COOKIE_NAME}={session[2]}"}


async def _open(state, run, person) -> tuple[str, str, str] | int:
    """The visitor's widget session, opened now if they have none; the status when it can't be."""
    session = visitor_session(person)
    if session is not None:
        return session
    r = await state.http.post(f"{_api(state, run)}/v1/website/sessions", headers={
        "Authorization": f"Bearer {run.phone_number_id}", "Origin": _origin(run)})
    if r.status_code not in (200, 201):
        return r.status_code
    # Read by hand: the cookie is Secure, and the runtime API is often reached over plain http.
    cookie = next(h.split(";", 1)[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie")
                  if h.startswith(f"{COOKIE_NAME}="))
    session = (r.json()["session_id"], r.json()["csrf_token"], cookie)
    person.address = " ".join(session)
    await store.update_person(state.session_factory, person.id, address=person.address)
    return session


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
        for q in args.get("questions") or [] if isinstance(q, dict)]}
    # A reply that ends in a question may carry its only words here.
    for key in ("message", "context"):
        if isinstance(args.get(key), str) and args[key].strip():
            body[key] = args[key]
    return body


async def _asked(sf, session_id: uuid.UUID) -> tuple[list[tuple[int, dict]], set[str]]:
    """The session's questions, oldest first, and the tool call ids that are closed."""
    async with sf() as db:
        asked = (await db.execute(
            sa.select(Event.id, Event.data)
            .where(Event.session_id == session_id, Event.type == "tool.call",
                   Event.data["name"].as_string() == ASK)
            .order_by(Event.id)
        )).all()
        closed = set((await db.execute(
            sa.select(Event.data["tool_call_id"].as_string())
            .where(Event.session_id == session_id, Event.type.in_(_CLOSED))
        )).scalars().all())
    return [(i, d) for i, d in asked], closed


async def _open_question(sf, session) -> dict | None:
    asked, closed = await _asked(sf, uuid.UUID(session[0]))
    waiting = [_question(d) for _, d in asked if d.get("tool_call_id") not in closed]
    return waiting[-1] if waiting else None


async def send(state, run, person, text: str) -> dict[str, Any]:
    """The visitor's message, posted as the widget posts it; an open question takes it as the answer."""
    sf = state.session_factory
    session = await _open(state, run, person)
    if isinstance(session, int):
        return {"delivered": False, "channels_status": session}
    question = await _open_question(sf, session)
    if question is not None:
        [only, *more] = question["questions"] or [{}]
        if more or not only.get("allow_other"):
            raise HTTPException(status_code=422, detail=(
                "the agent is waiting on its questions: answer them with a tap, "
                '{"answer": {"tool_call_id", "responses": [{"question", "answer"}]}}'))
        return await answer(state, run, person, question["tool_call_id"],
                            [{"question": only["prompt"], "answer": text}])
    r = await state.http.post(
        f"{_api(state, run)}/v1/website/sessions/{session[0]}/messages", json={"content": text},
        headers=_headers(run, session))
    delivered = r.status_code == 202
    await store.log_message(
        sf, run_id=run.id, person_id=person.id, direction="in", kind="text", body={"text": text},
        wamid=str(r.json()["event_id"]) if delivered else f"web.{secrets.token_hex(8)}",
        status="delivered" if delivered else "failed")
    return {"delivered": delivered, "channels_status": r.status_code}


async def answer(state, run, person, tool_call_id: str, responses: list[dict]) -> dict[str, Any]:
    """The visitor's answers to the agent's questions, submitted as the widget submits them."""
    sf = state.session_factory
    session = await _open(state, run, person)
    if isinstance(session, int):
        return {"delivered": False, "channels_status": session}
    r = await state.http.post(
        f"{_api(state, run)}/v1/website/sessions/{session[0]}/ask_user_question/{tool_call_id}/respond",
        json={"responses": responses}, headers=_headers(run, session))
    if r.status_code == 409:
        # Answered already, or the call stopped waiting: nothing to repeat, nothing wrong.
        await _close(sf, run, person, tool_call_id)
        return {"delivered": False, "channels_status": 409, "already_answered": True}
    if r.status_code != 201:
        raise HTTPException(status_code=422, detail=f"the answer was refused: {r.text[:300]}")
    await store.log_message(
        sf, run_id=run.id, person_id=person.id, direction="in", kind="answer",
        body={"text": "\n".join(f"{x['question']}: {x['answer']}" for x in responses),
              "answer": {"tool_call_id": tool_call_id, "responses": responses}},
        wamid=str(r.json()["event_id"]), status="delivered")
    await _close(sf, run, person, tool_call_id)
    return {"delivered": True, "channels_status": 201}


async def _close(sf, run, person, tool_call_id: str) -> None:
    for m in await store.thread(sf, person.id):
        if m.kind == "question" and m.body.get("tool_call_id") == tool_call_id and m.status != "answered":
            await store.set_status(sf, run.id, m.wamid, "answered")


async def collect(state, run, person) -> None:
    """Log each of the agent's replies and questions to the visitor once, keyed by event id."""
    session = visitor_session(person)
    if session is None:
        return
    sf, session_id = state.session_factory, uuid.UUID(session[0])
    logged = {m.wamid: m for m in await store.thread(sf, person.id) if m.direction == "out"}
    async with sf() as db:
        replies = (await db.execute(
            sa.select(Event.id, Event.data)
            .where(Event.session_id == session_id, Event.type == "llm.response")
        )).all()
    asked, closed = await _asked(sf, session_id)
    said = [(i, "text", {"text": c}, "delivered") for i, d in replies
            for c in [(d.get("message") or {}).get("content") or d.get("content")]
            if isinstance(c, str) and c.strip()]
    said += [(i, "question", _question(d), "answered" if d.get("tool_call_id") in closed else "")
             for i, d in asked]
    for event_id, kind, body, status in sorted(said, key=lambda s: s[0]):
        seen = logged.get(str(event_id))
        if seen is None:
            await store.log_message(sf, run_id=run.id, person_id=person.id, direction="out",
                                    kind=kind, body=body, wamid=str(event_id), status=status)
        elif kind == "question" and status and seen.status != status:
            await store.set_status(sf, run.id, seen.wamid, status)
