"""WhatsApp Flows: the agent's published forms, sent and read back.

ops projects each published flow the agent may send as ``whatsapp_forms`` in
the runtime config: ``{name, flow_id, title, cta, first_screen, description,
fields: [{name, label, type, description?, options?: [{id, title}]}]}``.

A send carries a ``flow_token`` of ``<form name>:<nonce>``.  The token is
recorded on the event that sent it, so a reply's token is trusted only when
that conversation's own log holds it: that binds it to the session and the
form without a key to share.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import sqlalchemy as sa

from surogates.db.models import Event

__all__ = [
    "flow_interactive",
    "form_by_name",
    "form_by_token_ref",
    "option_titles",
    "form_reply_text",
    "forms_from_payload",
    "new_flow_token",
    "sent_in_session",
]

#: Meta's cap on ``flow_cta``.
CTA_CHARS = 20
#: Meta's cap on an interactive message's body.
BODY_CHARS = 1024


def forms_from_payload(raw: Any) -> tuple[dict[str, Any], ...]:
    """The usable forms of a runtime-config ``whatsapp_forms`` list."""
    forms = []
    for form in raw or ():
        if isinstance(form, dict) and form.get("name") and form.get("flow_id"):
            forms.append({
                **form,
                "fields": [f for f in form.get("fields") or () if isinstance(f, dict) and f.get("name")],
            })
    return tuple(forms)


def form_by_name(forms: Any, name: str) -> dict[str, Any] | None:
    return next((f for f in forms or () if f.get("name") == name), None)


def form_by_token_ref(forms: Any, ref: str) -> dict[str, Any] | None:
    """The form a token names: by form name, or by flow id for a Program's
    template button, which ops projects as ``{index, flow_id}``."""
    return form_by_name(forms, ref) or next(
        (f for f in forms or () if str(f.get("flow_id")) == ref), None,
    )


def new_flow_token(form: str) -> str:
    return f"{form}:{secrets.token_urlsafe(12)}"


def _form_of_token(token: str) -> str:
    return token.split(":", 1)[0] if ":" in token else ""


def flow_interactive(
    form: dict[str, Any], *, message: str, token: str,
    prefill: dict[str, Any] | None = None, cta: str = "",
) -> dict[str, Any]:
    """The ``interactive`` object of a Flow message, opening on its first screen."""
    action_payload: dict[str, Any] = {"screen": form.get("first_screen") or ""}
    if prefill:
        action_payload["data"] = prefill
    return {
        "type": "flow",
        "body": {"text": message[:BODY_CHARS]},
        "action": {
            "name": "flow",
            "parameters": {
                "flow_message_version": "3",
                "flow_token": token,
                "flow_id": str(form["flow_id"]),
                "flow_cta": (cta or form.get("cta") or form.get("title") or "")[:CTA_CHARS],
                "flow_action": "navigate",
                "flow_action_payload": action_payload,
            },
        },
    }


def option_titles(field: dict[str, Any]) -> dict[str, str]:
    return {
        str(o["id"]): str(o.get("title") or o["id"])
        for o in field.get("options") or () if isinstance(o, dict) and o.get("id") is not None
    }


def _value(field: dict[str, Any], value: Any) -> str:
    """An answer as the person saw it: option titles, not the ids Meta returns."""
    if value is None or value == "" or value == []:
        return "—"
    # OptIn is the only boolean input; the care teams read these in Romanian.
    if isinstance(value, bool):
        return "Da" if value else "Nu"
    titles = option_titles(field)
    if isinstance(value, list):
        return ", ".join(titles.get(str(v), str(v)) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return titles.get(str(value), str(value))


def form_reply_text(form: dict[str, Any], fields: dict[str, Any]) -> str:
    """A submitted form as a header and one ``label: value`` line per answer.

    The form's own fields come first, in its order; anything else the flow
    returned follows under its key, so no answer is lost to a stale projection.
    A multi-line answer continues on lines indented by two spaces, so a reader
    never mistakes an answer line containing ": " for the next field.
    """
    known = {f["name"]: f for f in form.get("fields") or ()}
    labels = {name: f.get("label") or name for name, f in known.items()}
    answered = {k: v for k, v in fields.items() if k != "flow_token"}
    ordered = [k for k in labels if k in answered] + [k for k in answered if k not in labels]
    lines = [f"Form '{form.get('title') or form.get('name')}' submitted:"]
    lines += [
        f"{labels.get(k, k)}: " + "\n  ".join(_value(known.get(k, {}), answered[k]).splitlines())
        for k in ordered
    ]
    return "\n".join(lines)


async def sent_in_session(session_factory: Any, session_id: Any, token: str) -> str | None:
    """The form name of *token* if this session's log sent it, else ``None``."""
    form = _form_of_token(token or "")
    if not form:
        return None
    async with session_factory() as db:
        found = (
            await db.execute(
                sa.select(Event.id)
                .where(Event.session_id == session_id)
                .where(Event.data["flow_token"].astext == token)
                .limit(1)
            )
        ).scalar_one_or_none()
    return form if found is not None else None
