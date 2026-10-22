"""``start_case``: an intake agent starts a case of a type Keel grants it.

An agent that case types name as their intake agent, a front door that talks
with people, is offered ``start_case`` while Keel grants it a type: the
worker asks Keel at each wake (:func:`surogates.keel.intake.intake_case_types`).
Its schema takes only the granted types, and its description names each one
and the fields it requires (:func:`describe_intake`).  No session of a case
is offered it.

Each call is one request to Keel's door, keyed by the session and the tool
call: the same call sent again starts no second case, and Keel answers with
the case it started.  Keel also finds the case a session already started
with the same data, and refuses a session more cases than it allows.  Keel's
answer is the tool's result:

- the case started, or was found (201 or 200): ``Case <number> started (<type>).``;
- Keel refused it (403, 409 or 422): Keel's message and code, as an error the
  model can act on;
- Keel could not be reached (no answer, a 408, a 429 or another 5xx but a 503
  ``harness-off``): the model calls again;
- any other answer: an error naming the status, which no call again changes.

A start Keel could not take is never sent, and the model is told what to fix:
one larger than Keel takes, one holding NaN or an infinity, or a tool call id
too long for Keel's key.  An intake session is no step of a case: Keel is
never told how its turn ended.  No log line here carries the case's data.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Sequence
from typing import Any

from surogates.keel.client import BodyNotJSON, BodyTooLarge, keel_client
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

#: The longest ``Idempotency-Key`` Keel takes: ``intake:<session id>:<tool call id>``.
KEY_CHARS = 200

START_CASE_SCHEMA = ToolSchema(
    name="start_case",
    description=(
        "Start a case of one of the case types you may start, for the person you "
        "are talking with. Give the case type, and in 'data' the case's fields by "
        "name: every field the type requires, and any other it has that the person "
        "gave you. Keel checks the data: a refusal comes back as an error to fix "
        "before calling again. Calling again with the same data starts no second case."
    ),
    parameters={
        "type": "object",
        "properties": {
            "case_type": {"type": "string", "description": "The case type to start."},
            "data": {"type": "object", "description": "The case's fields, by name."},
        },
        "required": ["case_type", "data"],
    },
)


def describe_intake(schema: dict[str, Any], intake: Sequence[dict]) -> dict[str, Any]:
    """*schema* taking only the case types of *intake*, and naming each with its required fields.  Not mutated."""
    function = copy.deepcopy(schema["function"])
    function["parameters"]["properties"]["case_type"]["enum"] = [granted["case_type"] for granted in intake]
    types = "\n".join(
        f"- {granted['case_type']} ({granted['name']}): requires "
        + (", ".join(granted["required"]) if granted["required"] else "no fields")
        for granted in intake
    )
    function["description"] += f"\n\nThe case types you may start, and the fields each requires:\n{types}"
    return {**schema, "function": function}


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


async def handle_start_case(arguments: dict, **kwargs: Any) -> str:
    case_type = arguments.get("case_type")
    if not isinstance(case_type, str) or not case_type:
        return _error("case_type must name a case type you may start.")
    data = arguments.get("data")
    if not isinstance(data, dict):
        return _error("data must be an object of field to value.")
    call_id = kwargs.get("tool_call_id")
    if not call_id:
        return _error("start_case needs its tool call's id.")
    session_id = str(kwargs["session_id"])
    key = f"intake:{session_id}:{call_id}"
    if len(key) > KEY_CHARS:
        return _error("This tool call's id is longer than Keel takes, so no case was started.")

    client = keel_client()
    if client is None:
        return _error("This runtime has no link to Keel, so no case was started.")
    tenant = kwargs["tenant"]
    body = {
        "agent_id": str(kwargs["agent_id"]),
        "session_id": session_id,
        "user_id": str(tenant.user_id) if tenant.user_id is not None else None,
        "channel": kwargs.get("channel"),
        "case_type": case_type,
        "data": data,
    }
    try:
        answer = await client.start_case(tenant.org_id, body, idempotency_key=key)
    except BodyTooLarge as exc:
        return _error(
            f"The case is {exc.size:,} bytes, over the {exc.limit:,} bytes Keel takes. "
            "Shorten its data and call start_case again.",
        )
    except BodyNotJSON:
        return _error(
            "The case's data holds a number JSON cannot carry (NaN or Infinity). Give a real "
            "number, or leave the value out, and call start_case again.",
        )

    if answer.status in (200, 201):
        number = answer.data.get("number") if isinstance(answer.data, dict) else None
        return f"Case {number} started ({case_type})."
    if answer.status in (403, 409, 422):
        return _error(answer.message or "Keel refused to start the case.", code=answer.code)
    if answer.transient:
        return _error("Keel could not be reached; call start_case again.")
    logger.warning("Session %s: Keel refused to start a case (%s %s)", session_id, answer.status, answer.code or "")
    return _error(f"Keel refused this request ({answer.status}).")


def register(registry: Any) -> None:
    registry.register(name="start_case", schema=START_CASE_SCHEMA, handler=handle_start_case, toolset="keel")
