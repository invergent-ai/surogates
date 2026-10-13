"""``send_form``: open one of the agent's WhatsApp Flows for the person.

The form goes out through the session's ordered outbox, and the turn ends
there: nothing waits on it.  The person's answers arrive later as a message
of their own (see ``whatsapp_flows.form_reply_text``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from surogates.channels.whatsapp_flows import (
    BODY_CHARS,
    CTA_CHARS,
    flow_interactive,
    form_by_name,
    new_flow_token,
    option_titles,
)
from surogates.followup import store as followup_store
from surogates.followup.outreach import window_open
from surogates.session.events import EventType
from surogates.tools.registry import ToolSchema

#: Results start with this exactly when the form went to the outbox; the
#: harness ends the turn on it.
SENT = "Form sent."

SEND_FORM_SCHEMA = ToolSchema(
    name="send_form",
    description=(
        "Send the person a WhatsApp form to fill in, instead of asking its "
        "questions one by one. Pre-fill what you already know. The form's "
        "answers come back later as their next message, so your turn ends "
        "once it is sent: say everything that goes with it in 'message'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "form": {"type": "string", "description": "The form's name."},
            "message": {
                "type": "string",
                "maxLength": BODY_CHARS,
                "description": (
                    "The text shown above the form's button, in the person's "
                    "language: why you are sending it."
                ),
            },
            "prefill": {
                "type": "object",
                "description": "Optional answers already known, by field name.",
            },
            "cta": {
                "type": "string",
                "maxLength": CTA_CHARS,
                "description": (
                    "Optional button text in the person's language, at most "
                    f"{CTA_CHARS} characters. Defaults to the form's own."
                ),
            },
        },
        "required": ["form", "message"],
    },
)


def _describe_field(field: dict[str, Any]) -> str:
    detail = field.get("label") or field["name"]
    if field.get("description"):
        detail += f"; {field['description']}"
    titles = option_titles(field)
    if titles:
        detail += "; answers: " + " | ".join(titles.values())
    return f"{field['name']} ({detail})"


def describe_forms(schema: dict[str, Any], forms: Any) -> dict[str, Any]:
    """*schema* with ``form`` limited to *forms*, each described.  Not mutated."""
    names = [f["name"] for f in forms]
    listing = "\n".join(
        f"- {f['name']}: {f.get('title') or f['name']}"
        + (f" — {f['description']}" if f.get("description") else "")
        + " (fields: " + ", ".join(
            _describe_field(x) for x in f.get("fields") or ()
        ) + ")"
        for f in forms
    )
    function = dict(schema["function"])
    parameters = dict(function["parameters"])
    properties = dict(parameters["properties"])
    properties["form"] = {**properties["form"], "enum": names}
    parameters["properties"] = properties
    function["parameters"] = parameters
    function["description"] = f"{function['description']}\n\nForms:\n{listing}"
    return {**schema, "function": function}


def _forms_list(forms: Any) -> str:
    return ", ".join(f["name"] for f in forms) or "none"


async def handle_send_form(arguments: dict, **kwargs: Any) -> str:
    forms = (kwargs.get("session_config") or {}).get("whatsapp_forms") or ()
    name = str(arguments.get("form") or "").strip()
    form = form_by_name(forms, name)
    if form is None:
        return f"Not sent: there is no form '{name}'. This agent's forms: {_forms_list(forms)}."
    message = str(arguments.get("message") or "").strip()
    if not message:
        return "Not sent: 'message' is required, the text shown above the form."

    store = kwargs["session_store"]
    session_id = uuid.UUID(str(kwargs["session_id"]))
    session = await store.get_session(session_id)
    config = session.config or {}
    wa_id = str(config.get("whatsapp_channel_id") or "")
    sender = str(config.get("channel_identifier") or "")
    if session.channel != "whatsapp" or not wa_id or not sender:
        return "Not sent: forms can only be sent in a WhatsApp conversation."

    # Outside the 24-hour window only an approved template reaches the
    # person, and Meta would refuse this message.
    last = await followup_store.last_inbound(
        kwargs["session_factory"], org_id=session.org_id, platform="whatsapp",
        platform_user_id=wa_id, channel_identifier=sender,
    )
    if not window_open(last, datetime.now(timezone.utc)):
        return (
            "Not sent: the person has not written in the last 24 hours, so "
            "WhatsApp lets only an approved template reach them. A Program's "
            "template can carry the form instead."
        )

    raw_prefill = arguments.get("prefill") or {}
    if not isinstance(raw_prefill, dict):
        return "Not sent: 'prefill' must be an object of field name to value."
    known = {f["name"] for f in form.get("fields") or ()}
    prefill = {k: v for k, v in raw_prefill.items() if k in known and v not in (None, "")}
    ignored = sorted(set(raw_prefill) - known)

    token = new_flow_token(form["name"])
    await store.emit_event(session_id, EventType.CHANNEL_FORM, {
        "form": form["name"],
        "flow_token": token,
        "interactive": flow_interactive(
            form, message=message, token=token, prefill=prefill,
            cta=str(arguments.get("cta") or "").strip(),
        ),
    })
    result = (
        f"{SENT} End your turn now without writing anything else; the "
        "person's answers will arrive as their next message."
    )
    if ignored:
        result += f" Not pre-filled (no such field): {', '.join(ignored)}."
    return result


def register(registry: Any) -> None:
    registry.register(
        name="send_form", schema=SEND_FORM_SCHEMA, handler=handle_send_form,
        toolset="channels",
    )

