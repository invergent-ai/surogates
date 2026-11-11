"""Autopilot as a website visitor.

It opens a widget session and writes through the public routes the widget
calls, so the agent meets it as it meets anyone on the site: origin check,
rate limit, message cap and all.  The agent's replies are read from the
session's events, the same ``llm.response`` texts the widget shows, straight
from the runtime DB: a poll every tick is cheaper than an SSE stream.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import sqlalchemy as sa

from surogates.channels.website_session import COOKIE_NAME, CSRF_HEADER_NAME
from surogates.db.models import Event
from surogates.simulation import store


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


async def send(state, run, person, text: str) -> dict[str, Any]:
    """The visitor's message, posted as the widget posts it."""
    sf, api = state.session_factory, _api(state, run)
    origin = (run.routing_config.get("allowed_origins") or [""])[0]
    session = visitor_session(person)
    if session is None:
        r = await state.http.post(f"{api}/v1/website/sessions", headers={
            "Authorization": f"Bearer {run.phone_number_id}", "Origin": origin})
        if r.status_code not in (200, 201):
            return {"delivered": False, "channels_status": r.status_code}
        # Read by hand: the cookie is Secure, and the runtime API is often reached over plain http.
        cookie = next(h.split(";", 1)[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie")
                      if h.startswith(f"{COOKIE_NAME}="))
        session = (r.json()["session_id"], r.json()["csrf_token"], cookie)
        person.address = " ".join(session)
        await store.update_person(sf, person.id, address=person.address)
    session_id, csrf, cookie = session
    r = await state.http.post(
        f"{api}/v1/website/sessions/{session_id}/messages", json={"content": text},
        headers={"Origin": origin, CSRF_HEADER_NAME: csrf, "Cookie": f"{COOKIE_NAME}={cookie}"})
    delivered = r.status_code == 202
    await store.log_message(
        sf, run_id=run.id, person_id=person.id, direction="in", kind="text", body={"text": text},
        wamid=str(r.json()["event_id"]) if delivered else f"web.{secrets.token_hex(8)}",
        status="delivered" if delivered else "failed")
    return {"delivered": delivered, "channels_status": r.status_code}


async def collect(state, run, person) -> None:
    """Log each of the agent's replies to the visitor once, keyed by its event id."""
    session = visitor_session(person)
    if session is None:
        return
    sf = state.session_factory
    seen = {m.wamid for m in await store.thread(sf, person.id) if m.direction == "out"}
    async with sf() as db:
        replies = (await db.execute(
            sa.select(Event.id, Event.data)
            .where(Event.session_id == uuid.UUID(session[0]), Event.type == "llm.response")
            .order_by(Event.id)
        )).all()
    for event_id, data in replies:
        content = (data.get("message") or {}).get("content") or data.get("content")
        if str(event_id) in seen or not isinstance(content, str) or not content.strip():
            continue
        await store.log_message(sf, run_id=run.id, person_id=person.id, direction="out", kind="text",
                                body={"text": content}, wamid=str(event_id), status="delivered")
