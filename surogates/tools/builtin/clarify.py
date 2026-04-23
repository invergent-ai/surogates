"""Clarify tool -- interactive multi-question prompts.

The tool lets the agent present a batch of clarifying questions to the user
through the web chat widget.  Each question carries up to four labeled
choices (label + description) and may optionally accept an "Other" free-form
answer.  The widget collects every answer and submits them as a single
batch, so the agent receives one structured response for the whole ask.

Round-trip
==========

1. The LLM invokes ``clarify`` with a ``questions`` array.
2. ``tool_exec`` emits ``TOOL_CALL`` with ``tool_call_id`` and the spec.
3. The frontend renders the widget from the tool-call arguments.
4. The user submits via ``POST /v1/sessions/{id}/clarify/{tool_call_id}/respond``.
5. The endpoint emits :attr:`~surogates.session.events.EventType.CLARIFY_RESPONSE`.
6. This handler polls the event log for the matching response, renewing
   the session lease to prevent expiry, and returns the answers as JSON.
7. If the user pauses the session instead of answering, the handler exits
   with ``cancelled: true`` so the LLM sees a clean termination.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# Schema limits ------------------------------------------------------------
MAX_QUESTIONS = 5
MAX_CHOICES_PER_QUESTION = 4
MAX_PROMPT_LENGTH = 1000
MAX_LABEL_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 500

# Polling / lease renewal --------------------------------------------------
_POLL_INTERVAL_SECONDS = 1.0
# Keep well under :data:`surogates.harness.loop._LEASE_TTL_SECONDS` (60s).
_LEASE_RENEW_INTERVAL_SECONDS = 30.0
# Hard cap on how long we keep the worker parked on a single clarify call.
# Past this, we emit a timeout response so the LLM can move on.
_MAX_WAIT_SECONDS = 30 * 60  # 30 minutes


CLARIFY_DESCRIPTION = (
    "Ask the user one or more clarifying questions before proceeding.  Each "
    "question is rendered as a tab in the chat widget; the user picks an "
    "answer per question (or types an 'Other' response) and submits the "
    "batch at once.\n\n"
    "Each question has:\n"
    "- ``prompt`` (required) -- the question text.\n"
    "- ``choices`` (optional) -- up to 4 labeled options.  Each choice is "
    "an object with ``label`` (short) and an optional ``description`` "
    "(one-line rationale).  Omit to ask an open-ended question.\n"
    "- ``allow_other`` (optional, default true) -- when true the widget "
    "appends an 'Other' option with a text field.\n\n"
    "Use when:\n"
    "- The task is ambiguous and the user must choose an approach.\n"
    "- A decision has meaningful trade-offs worth surfacing.\n"
    "- You need to collect multiple decisions in one round-trip.\n\n"
    "Do NOT use for simple yes/no confirmation of dangerous commands (the "
    "terminal tool handles that).  Prefer a reasonable default yourself "
    "when the decision is low-stakes.  If the user pauses the session "
    "instead of answering, you will receive ``cancelled: true`` -- stop and "
    "wait for further instructions."
)


CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_QUESTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The question to present.",
                    },
                    "choices": {
                        "type": "array",
                        "maxItems": MAX_CHOICES_PER_QUESTION,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Short answer label.",
                                },
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "Optional one-line rationale for "
                                        "the choice."
                                    ),
                                },
                            },
                            "required": ["label"],
                        },
                        "description": (
                            "Up to 4 labeled options.  Omit for an "
                            "open-ended question."
                        ),
                    },
                    "allow_other": {
                        "type": "boolean",
                        "description": (
                            "When true, the widget appends an 'Other' "
                            "choice with a free-form text field.  Defaults "
                            "to true."
                        ),
                    },
                },
                "required": ["prompt"],
            },
            "description": (
                f"Between 1 and {MAX_QUESTIONS} questions to ask in a "
                "single batch.  Each is rendered as a tab in the widget."
            ),
        },
    },
    "required": ["questions"],
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ClarifySchemaError(ValueError):
    """Raised when the LLM-supplied ``questions`` payload is invalid."""


def _validate_questions(raw: Any) -> list[dict[str, Any]]:
    """Return a normalised list of questions, or raise :class:`ClarifySchemaError`."""
    if not isinstance(raw, list):
        raise ClarifySchemaError("`questions` must be an array of objects.")
    if not raw:
        raise ClarifySchemaError("`questions` must not be empty.")
    if len(raw) > MAX_QUESTIONS:
        raise ClarifySchemaError(
            f"`questions` supports at most {MAX_QUESTIONS} entries.",
        )

    normalised: list[dict[str, Any]] = []
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            raise ClarifySchemaError(f"questions[{i}] must be an object.")

        prompt = q.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ClarifySchemaError(f"questions[{i}].prompt is required.")
        prompt = prompt.strip()[:MAX_PROMPT_LENGTH]

        raw_choices = q.get("choices")
        choices: list[dict[str, str]] = []
        if raw_choices is not None:
            if not isinstance(raw_choices, list):
                raise ClarifySchemaError(
                    f"questions[{i}].choices must be an array.",
                )
            if len(raw_choices) > MAX_CHOICES_PER_QUESTION:
                raise ClarifySchemaError(
                    f"questions[{i}].choices supports at most "
                    f"{MAX_CHOICES_PER_QUESTION} entries.",
                )
            for j, c in enumerate(raw_choices):
                if not isinstance(c, dict):
                    raise ClarifySchemaError(
                        f"questions[{i}].choices[{j}] must be an object.",
                    )
                label = c.get("label")
                if not isinstance(label, str) or not label.strip():
                    raise ClarifySchemaError(
                        f"questions[{i}].choices[{j}].label is required.",
                    )
                entry: dict[str, str] = {
                    "label": label.strip()[:MAX_LABEL_LENGTH],
                }
                desc = c.get("description")
                if isinstance(desc, str) and desc.strip():
                    entry["description"] = desc.strip()[:MAX_DESCRIPTION_LENGTH]
                choices.append(entry)

        allow_other = q.get("allow_other", True)
        if not isinstance(allow_other, bool):
            allow_other = True

        item: dict[str, Any] = {
            "prompt": prompt,
            "allow_other": allow_other,
        }
        if choices:
            item["choices"] = choices
        normalised.append(item)

    return normalised


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES = {"paused", "completed", "failed", "archived"}


async def _wait_for_response(
    *,
    session_id: UUID,
    tool_call_id: str,
    session_store: Any,
    lease_token: Any | None,
) -> dict[str, Any]:
    """Poll for the matching ``CLARIFY_RESPONSE`` event or a session stop.

    Returns ``{"responses": [...], "cancelled": False}`` on success, or
    ``{"cancelled": True, "reason": <why>}`` when the user stopped the
    chat (session paused/completed/failed) or we hit the wait cap.

    Only ``CLARIFY_RESPONSE`` events are read from the log -- filtering by
    ``tool_call_id`` is enough because each id is unique per LLM call.
    Cancel detection uses the session's current status rather than an
    event-log scan so we never confuse a fresh pause with a historical one.

    The session lease is renewed on a fixed cadence so the orchestrator
    does not steal the session while the user deliberates.
    """
    deadline = asyncio.get_running_loop().time() + _MAX_WAIT_SECONDS
    next_renew = asyncio.get_running_loop().time() + _LEASE_RENEW_INTERVAL_SECONDS
    cursor = 0

    while True:
        now = asyncio.get_running_loop().time()
        if now >= deadline:
            logger.warning(
                "Clarify tool %s timed out after %ds", tool_call_id,
                _MAX_WAIT_SECONDS,
            )
            return {"cancelled": True, "reason": "timeout"}

        # Lease renewal keeps ownership while the user composes an answer.
        if lease_token is not None and now >= next_renew:
            try:
                await session_store.renew_lease(
                    session_id, lease_token, ttl_seconds=60,
                )
            except Exception:
                logger.warning(
                    "Failed to renew lease during clarify wait for %s",
                    session_id, exc_info=True,
                )
            next_renew = now + _LEASE_RENEW_INTERVAL_SECONDS

        # 1. Look for this tool call's response.
        events = await session_store.get_events(
            session_id,
            after=cursor,
            types=[EventType.CLARIFY_RESPONSE],
        )
        for event in events:
            cursor = max(cursor, event.id)
            data = event.data or {}
            if data.get("tool_call_id") == tool_call_id:
                responses = data.get("responses")
                if isinstance(responses, list):
                    return {"responses": responses, "cancelled": False}
                return {"cancelled": True, "reason": "malformed_response"}

        # 2. Has the session been stopped?  Status is the authoritative
        #    current state -- the pause endpoint both emits SESSION_PAUSE
        #    and flips the row to ``paused`` atomically, so a transient
        #    event from a prior pause/resume cycle cannot fool us.
        try:
            session = await session_store.get_session(session_id)
        except Exception:
            logger.debug(
                "Session lookup failed during clarify wait for %s",
                session_id, exc_info=True,
            )
            session = None
        if session is not None and session.status in _TERMINAL_STATUSES:
            return {"cancelled": True, "reason": f"session.{session.status}"}

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Handler + registration
# ---------------------------------------------------------------------------


async def _clarify_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Async handler for the clarify tool.

    Required kwargs (injected by :mod:`surogates.harness.tool_exec`):

    - ``session_id`` -- UUID or string, the active session.
    - ``session_store`` -- :class:`~surogates.session.store.SessionStore`.
    - ``tool_call_id`` -- the LLM-supplied tool-call identifier.

    Optional:

    - ``lease_token`` -- current lease token, used to renew during the wait.
    """
    session_store = kwargs.get("session_store")
    tool_call_id = kwargs.get("tool_call_id")
    raw_session_id = kwargs.get("session_id")
    lease_token = kwargs.get("lease_token")

    if session_store is None or not tool_call_id or raw_session_id is None:
        return json.dumps(
            {"error": "Clarify tool requires a session context."},
            ensure_ascii=False,
        )
    session_id = (
        raw_session_id if isinstance(raw_session_id, UUID)
        else UUID(str(raw_session_id))
    )

    try:
        questions = _validate_questions(arguments.get("questions"))
    except ClarifySchemaError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    outcome = await _wait_for_response(
        session_id=session_id,
        tool_call_id=str(tool_call_id),
        session_store=session_store,
        lease_token=lease_token,
    )

    if outcome.get("cancelled"):
        return json.dumps(
            {
                "cancelled": True,
                "reason": outcome.get("reason", "cancelled"),
                "questions_asked": questions,
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "cancelled": False,
            "responses": outcome["responses"],
            "questions_asked": questions,
        },
        ensure_ascii=False,
    )


def register(registry: ToolRegistry) -> None:
    """Register the clarify tool."""
    registry.register(
        name="clarify",
        schema=ToolSchema(
            name="clarify",
            description=CLARIFY_DESCRIPTION,
            parameters=CLARIFY_SCHEMA,
        ),
        handler=_clarify_handler,
        toolset="clarify",
    )