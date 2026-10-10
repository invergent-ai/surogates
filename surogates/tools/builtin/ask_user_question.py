"""ask_user_question tool -- interactive multi-question prompts.

The tool lets the agent present a batch of clarifying questions to the user
through the web chat widget.  Each question carries up to four labeled
choices (label + description) and may optionally accept an "Other" free-form
answer.  The widget collects every answer and submits them as a single
batch, so the agent receives one structured response for the whole ask.

Round-trip
==========

1. The LLM invokes ``ask_user_question`` with a ``questions`` array.
2. ``tool_exec`` emits ``TOOL_CALL`` with ``tool_call_id`` and the spec.
3. The frontend renders the widget from the tool-call arguments.
4. The user submits via
   ``POST /v1/sessions/{id}/ask_user_question/{tool_call_id}/respond``.
5. The endpoint emits
   :attr:`~surogates.session.events.EventType.ASK_USER_QUESTION_RESPONSE`.
6. This handler waits for the matching response, renewing the session lease
   to prevent expiry, and returns the answers as JSON.  It wakes on the
   session's Redis nudge when there is one, and polls the event log when not.
7. If the user pauses the session instead of answering, the handler exits
   with ``cancelled: true`` so the LLM sees a clean termination.  So it
   does, with ``reason: "dismissed"``, when the user types a command
   instead: the harness ends the turn there and runs the command.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from uuid import UUID

from surogates.harness.slash_skill import names_builtin_command
from surogates.runtime.turn_slots import turn_waiting
from surogates.session.events import EventType
from surogates.session.interactive_input import expire_input_request
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
# How long a wait goes without a nudge before it checks again anyway: a lost
# message only delays the answer by this much.
_RECHECK_INTERVAL_SECONDS = 5.0
# Keep well under :data:`surogates.harness.loop._LEASE_TTL_SECONDS` (60s).
_LEASE_RENEW_INTERVAL_SECONDS = 30.0
# Hard cap on how long we keep the worker parked on a single ask call.
# Past this, we emit a timeout response so the LLM can move on.
# Public: channel/web surfaces that convert free-text messages into
# answers bound their staleness window to this — an inbox row older
# than the wait cannot belong to a live tool call.
ASK_USER_QUESTION_MAX_WAIT_SECONDS = 30 * 60  # 30 minutes
_MAX_WAIT_SECONDS = ASK_USER_QUESTION_MAX_WAIT_SECONDS

#: Why a question its user typed a command over went unanswered, for the model.
DISMISSED = "dismissed"
DISMISSED_BY_A_COMMAND = (
    "The user typed a command instead of answering, so the question was not answered. "
    "Ask it again if you still need the answer."
)


ASK_USER_QUESTION_DESCRIPTION = (
    "Ask the user one or more questions and wait for their answers before "
    "continuing.  Use this only when you are blocked on a decision that is "
    "genuinely the user's to make — one you cannot settle from the "
    "request, the files, or a sensible default.  Reserve it for decisions "
    "where the answer changes what you do next, not for choices with a "
    "conventional default or facts you can verify yourself; in those "
    "cases pick the obvious option, say so, and carry on.  Asking "
    "suspends the session until a human replies, so a needless question "
    "is a stall, not a courtesy.\n\n"
    "Each question is rendered as a tab in the chat "
    "widget; the user picks an answer per question (or types an 'Other' "
    "response) and submits the batch at once.\n\n"
    "Each question has:\n"
    "- ``prompt`` (required) -- the question text.\n"
    "- ``choices`` (optional) -- up to 4 labeled options.  Each choice is "
    "an object with ``label`` (short) and an optional ``description`` "
    "(one-line rationale).  Omit to ask an open-ended question.\n"
    "- ``allow_other`` (optional, default true) -- when true the widget "
    "appends an 'Other' option with a text field.\n\n"
    "Use when:\n"
    "- Readings of the request differ enough to lead to materially "
    "different work, and the files do not settle which one is meant.\n"
    "- Only the user holds the information, and guessing wrong wastes "
    "the work rather than merely re-doing a step.\n"
    "- Several such decisions are open at once — collect them in one "
    "round-trip rather than stalling repeatedly.\n\n"
    "Do NOT use for simple yes/no confirmation of dangerous commands (the "
    "terminal tool handles that).  If the user pauses the session "
    "instead of answering, you will receive ``cancelled: true`` -- stop and "
    "wait for further instructions."
)


ASK_USER_QUESTION_SCHEMA = {
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


class AskUserQuestionSchemaError(ValueError):
    """Raised when the LLM-supplied ``questions`` payload is invalid."""


def _validate_questions(raw: Any) -> list[dict[str, Any]]:
    """Return a normalised list of questions, or raise :class:`AskUserQuestionSchemaError`."""
    if not isinstance(raw, list):
        raise AskUserQuestionSchemaError("`questions` must be an array of objects.")
    if not raw:
        raise AskUserQuestionSchemaError("`questions` must not be empty.")
    if len(raw) > MAX_QUESTIONS:
        raise AskUserQuestionSchemaError(
            f"`questions` supports at most {MAX_QUESTIONS} entries.",
        )

    normalised: list[dict[str, Any]] = []
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            raise AskUserQuestionSchemaError(f"questions[{i}] must be an object.")

        prompt = q.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise AskUserQuestionSchemaError(f"questions[{i}].prompt is required.")
        prompt = prompt.strip()[:MAX_PROMPT_LENGTH]

        raw_choices = q.get("choices")
        choices: list[dict[str, str]] = []
        if raw_choices is not None:
            if not isinstance(raw_choices, list):
                raise AskUserQuestionSchemaError(
                    f"questions[{i}].choices must be an array.",
                )
            if len(raw_choices) > MAX_CHOICES_PER_QUESTION:
                raise AskUserQuestionSchemaError(
                    f"questions[{i}].choices supports at most "
                    f"{MAX_CHOICES_PER_QUESTION} entries.",
                )
            for j, c in enumerate(raw_choices):
                if not isinstance(c, dict):
                    raise AskUserQuestionSchemaError(
                        f"questions[{i}].choices[{j}] must be an object.",
                    )
                label = c.get("label")
                if not isinstance(label, str) or not label.strip():
                    raise AskUserQuestionSchemaError(
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
# Waiting
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES = {"paused", "completed", "failed", "archived"}


async def _subscribe(redis: Any, session_id: UUID) -> Any | None:
    """Listen for the session's event nudges before the first check, so none is missed."""
    if redis is None:
        return None
    # ponytail: one pub/sub connection per waiting question, held up to its
    # 30-minute cap; share one subscriber per worker when they number in thousands.
    pubsub = redis.pubsub()
    try:
        async with asyncio.timeout(_RECHECK_INTERVAL_SECONDS):
            await pubsub.subscribe(f"surogates:session:{session_id}")
    except BaseException as exc:
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        if not isinstance(exc, Exception):
            raise  # cancelled: the caller's own cleanup runs
        logger.warning("ask_user_question: no event nudges for %s; polling", session_id, exc_info=True)
        return None
    return pubsub


async def _until_nudged(pubsub: Any | None, *, within: float) -> None:
    """Return once any event of the session is announced, or *within* seconds pass."""
    if pubsub is None:
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, within))
        return
    try:
        async with asyncio.timeout(within + _RECHECK_INTERVAL_SECONDS):
            await pubsub.get_message(ignore_subscribe_messages=True, timeout=within)
    except Exception:
        logger.debug("ask_user_question: nudge lost; polling once", exc_info=True)
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, within))


def _typed_a_command(event: Any) -> bool:
    """Whether *event*, a ``user.message``, is a built-in command its user typed, whatever its arguments."""
    data = event.data or {}
    content = data.get("content")
    return not data.get("synthetic") and isinstance(content, str) and names_builtin_command(content)


async def _wait_for_response(
    *,
    session_id: UUID,
    tool_call_id: str,
    session_store: Any,
    lease_token: Any | None,
    redis: Any | None = None,
    asked_at: int = 0,
) -> dict[str, Any]:
    """Wait for the matching ``ASK_USER_QUESTION_RESPONSE`` event, a
    command its user typed instead, or a session stop.

    With *redis* it wakes on the session's event nudge and rechecks at a
    bounded interval, so a lost nudge only delays the answer.  Without
    *redis* (or when the subscription fails) it polls the event log.

    Returns ``{"responses": [...], "cancelled": False}`` on success, or
    ``{"cancelled": True, "reason": <why>}`` when the user stopped the
    chat (session paused/completed/failed), typed a command after event
    *asked_at*, the question's, or we hit the wait cap.

    A command is never a question's answer, and its user waits for it: it
    dismisses the question, and the harness runs it once the turn has
    ended.  Not where the same read finds the answer too: an answer is
    never lost.  Any other message is the question's answer where its
    route takes it for one, and waits for the turn otherwise.

    Only ``ASK_USER_QUESTION_RESPONSE`` events are read from the log --
    filtering by ``tool_call_id`` is enough because each id is unique
    per LLM call -- and the messages written since the question, both in
    one read.
    Cancel detection uses the session's current status rather than an
    event-log scan so we never confuse a fresh pause with a historical one.

    The session lease is renewed on a fixed cadence so the orchestrator
    does not steal the session while the user deliberates.
    """
    deadline = asyncio.get_running_loop().time() + _MAX_WAIT_SECONDS
    next_renew = asyncio.get_running_loop().time() + _LEASE_RENEW_INTERVAL_SECONDS
    cursor = 0
    said = asked_at

    # Subscribe before the first check: an answer landing in between still
    # wakes the wait.
    pubsub = await _subscribe(redis, session_id)
    try:
        while True:
            now = asyncio.get_running_loop().time()
            if now >= deadline:
                logger.warning(
                    "ask_user_question tool %s timed out after %ds", tool_call_id,
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
                        "Failed to renew lease during ask_user_question wait for %s",
                        session_id, exc_info=True,
                    )
                next_renew = now + _LEASE_RENEW_INTERVAL_SECONDS

            # 1. Look for this tool call's response, and for a command its
            #    user typed instead, in one read of the log.  An answer it
            #    finds is taken, whichever came first: its route told its
            #    user it was, and the command waits for the turn's end.
            events = await session_store.get_events(
                session_id,
                after=min(cursor, said),
                types=[EventType.ASK_USER_QUESTION_RESPONSE, EventType.USER_MESSAGE],
            )
            commanded = False
            for event in events:
                data = event.data or {}
                if event.type == EventType.USER_MESSAGE.value:
                    commanded = commanded or (event.id > said and _typed_a_command(event))
                elif data.get("tool_call_id") == tool_call_id:
                    responses = data.get("responses")
                    if isinstance(responses, list):
                        return {"responses": responses, "cancelled": False}
                    return {"cancelled": True, "reason": "malformed_response"}
            if commanded:
                return {"cancelled": True, "reason": DISMISSED}
            cursor = said = max([cursor, said, *(event.id for event in events)])

            # 2. Has the session been stopped?  Status is the authoritative
            #    current state -- the pause endpoint both emits SESSION_PAUSE
            #    and flips the row to ``paused`` atomically, so a transient
            #    event from a prior pause/resume cycle cannot fool us.
            try:
                session = await session_store.get_session(session_id)
            except Exception:
                logger.debug(
                    "Session lookup failed during ask_user_question wait for %s",
                    session_id, exc_info=True,
                )
                session = None
            if session is not None and session.status in _TERMINAL_STATUSES:
                return {"cancelled": True, "reason": f"session.{session.status}"}

            remaining = deadline - asyncio.get_running_loop().time()
            await _until_nudged(
                pubsub, within=max(0.0, min(_RECHECK_INTERVAL_SECONDS, remaining)),
            )
    finally:
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.aclose()


# ---------------------------------------------------------------------------
# Handler + registration
# ---------------------------------------------------------------------------


async def _ask_user_question_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Async handler for the ask_user_question tool.

    Required kwargs (injected by :mod:`surogates.harness.tool_exec`):

    - ``session_id`` -- UUID or string, the active session.
    - ``session_store`` -- :class:`~surogates.session.store.SessionStore`.
    - ``tool_call_id`` -- the LLM-supplied tool-call identifier.

    Optional:

    - ``lease_token`` -- current lease token, used to renew during the wait.
    - ``redis`` -- the worker's Redis client; the wait wakes on the session's
      event nudge instead of polling.
    """
    session_store = kwargs.get("session_store")
    tool_call_id = kwargs.get("tool_call_id")
    raw_session_id = kwargs.get("session_id")
    lease_token = kwargs.get("lease_token")

    if session_store is None or not tool_call_id or raw_session_id is None:
        return json.dumps(
            {"error": "ask_user_question tool requires a session context."},
            ensure_ascii=False,
        )
    session_id = (
        raw_session_id if isinstance(raw_session_id, UUID)
        else UUID(str(raw_session_id))
    )

    try:
        questions = _validate_questions(arguments.get("questions"))
    except AskUserQuestionSchemaError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    asked_at = await session_store.emit_event(
        session_id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": str(tool_call_id),
            "questions": questions,
            "context": arguments.get("context", ""),
        },
    )

    # A person answering consumes no worker capacity, and can take 30
    # minutes: the turn gives its slots back while it waits for them.
    async with turn_waiting():
        outcome = await _wait_for_response(
            session_id=session_id,
            tool_call_id=str(tool_call_id),
            session_store=session_store,
            lease_token=lease_token,
            redis=kwargs.get("redis"),
            asked_at=asked_at,
        )

    if outcome.get("cancelled"):
        # Nothing is waiting on the answer any more, so the inbox item
        # stops offering to take one. Best-effort: the sweeper catches a
        # row left behind by a worker that died before getting here.
        try:
            await expire_input_request(
                session_store,
                session_id=session_id,
                tool_call_id=str(tool_call_id),
            )
        except Exception:
            logger.warning(
                "Failed to expire the inbox item for ask_user_question %s",
                tool_call_id, exc_info=True,
            )
        reason = outcome.get("reason", "cancelled")
        return json.dumps(
            {
                "cancelled": True,
                "reason": reason,
                **({"detail": DISMISSED_BY_A_COMMAND} if reason == DISMISSED else {}),
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
    """Register the ask_user_question tool."""
    registry.register(
        name="ask_user_question",
        schema=ToolSchema(
            name="ask_user_question",
            description=ASK_USER_QUESTION_DESCRIPTION,
            parameters=ASK_USER_QUESTION_SCHEMA,
        ),
        handler=_ask_user_question_handler,
        toolset="ask_user_question",
    )
