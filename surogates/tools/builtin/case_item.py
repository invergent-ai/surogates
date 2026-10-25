"""``complete_item`` and ``return_item``: an agent's session on a work item reports the item to Keel.

Only the session Keel started for one work item of a case is offered them.
``complete_item`` acts on the item: it completes its step or casts its vote
with one of the item's outcomes, the outputs that outcome requires and a
source for each value, or answers its raised hand with one of its choices or
in words.  ``return_item`` gives the item back to whoever gave it, with the
reason.  Both go to the item routes of Keel's door, and Keel's answer
decides what the session does next:

- Keel recorded the report: the item is marked (``completed`` or
  ``returned``), its inputs leave the session, and the result starts with
  :data:`ITEM_ENDED`; the harness ends the turn.
- The item is no longer this session's (a 409 ``not-running``): the result
  starts with :data:`ITEM_ENDED`, and nothing is marked.
- Keel rejected the report (another 409, with the engine's own code): the
  result is the rejection, and the model fixes its report and calls again,
  or returns the item.
- Keel could not be reached (no answer, a 408, a 429 or a 5xx other than a
  503 ``harness-off``): the model calls again.  A repeated call id gets
  Keel's first answer.
- Keel refused the report for good (any other answer), or this runtime has
  no link to Keel: Keel is told the item failed, its inputs leave the
  session, it is marked ``stopped``, and the result starts with
  :data:`ITEM_STOPPED`; the harness ends the turn, and people take the item.

A report Keel could not read is never sent, as a step's is not, and the
checks are the step tools' own.  Once a report ended the item, a later
report in the same wake is answered here, without Keel.  No log line here
carries the case's data.

A turn of the item's session that ends with no report ending the item is
told to Keel through :func:`report_item_no_outcome`, and Keel gives the item
back; a session of the item that fails, at its turn's end or in the
dispatcher, through :func:`report_item_failed`.  Neither tells Keel of an
item that is marked or over (:func:`item_awaiting_report`).
"""

from __future__ import annotations

import copy
import logging
from typing import Any
from uuid import UUID

from surogates.keel.client import KeelAnswer, KeelClient, keel_client
from surogates.keel.items import ItemContract, item_contract_in
from surogates.keel.steps import CASE_ITEM_KEY, ITEM_ENDED, ITEM_STOPPED
from surogates.tools.builtin.case_step import (
    _STOPPED,
    _began,
    _call_id_refused,
    _completion,
    _ended_in_this_wake,
    _error,
    _mark,
    _redact,
    _rejection,
    _remember_the_end,
    _sent,
    _specialise,
    _tell_keel,
    work_awaiting_report,
)
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

#: The longest reason Keel takes for a returned item.
REASON_CHARS = 2000

_NOT_AN_ITEM = "This session works no work item of a case: there is no item to report."
_NO_LONGER = f"{ITEM_ENDED} the item is no longer this session's."
#: The door's route for each tool, what the model reads once Keel recorded it, and the item's mark.
_ROUTES = {"complete_item": "item/complete", "return_item": "item/return"}
_RECORDED = {
    "complete_item": (f"{ITEM_ENDED} Keel recorded it.", "completed"),
    "return_item": (f"{ITEM_ENDED} the item went back.", "returned"),
}
#: A step's and a vote's properties of ``complete_item``; a raised hand's.
_COMPLETION = ("outcome", "output", "sources")
_ANSWER = ("choice", "answer", "note")

#: Where a value comes from, as the model names it.
_PLACES = (
    "a knowledge page as {\"kb_id\", \"path\"}, a file in /workspace as {\"file\"}, one of "
    "the item's inputs as {\"input\"}, or a web page as {\"url\"}"
)
# Where a raised hand's answer comes from.
_ANSWER_SOURCES = {
    "type": "array",
    "items": {"type": "object"},
    "description": f"Optional: where your answer comes from, each an object: {_PLACES}.",
}

COMPLETE_ITEM_SCHEMA = ToolSchema(
    name="complete_item",
    description=(
        "Act on this session's work item of the case: complete its step, cast its "
        "vote, or answer its raised hand. A step or a vote ends with one of its "
        "outcomes: give, in 'output', every output the outcome requires, and in "
        "'sources' where each value comes from. A raised hand takes one of its "
        "choices, or your answer in words, with an optional note and your sources. "
        "Keel checks it: a refusal comes back as an error to fix before calling "
        "again, or to give the item back with return_item. Once Keel records it, "
        "the item is over for this session and your turn ends."
    ),
    parameters={
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "description": "A step's or a vote's: the outcome it ends with."},
            "output": {"type": "object", "description": "A step's or a vote's: its outputs, by field."},
            "sources": {
                "type": "object",
                "description": (
                    "For each output field, a non-empty list of where its value comes from, "
                    f"each an object: {_PLACES}."
                ),
                "additionalProperties": {"type": "array", "minItems": 1, "items": {"type": "object"}},
            },
            "choice": {"type": "string", "description": "A raised hand's: the one of its choices you pick."},
            "answer": {"type": "string", "description": "A raised hand's: your answer in words, when you pick no choice."},
            "note": {"type": "string", "description": "A raised hand's: an optional note with your answer."},
        },
    },
)

RETURN_ITEM_SCHEMA = ToolSchema(
    name="return_item",
    description=(
        "Give this session's work item of the case back to whoever gave it to you, "
        "with the reason you cannot act on it. Once Keel takes it back, the item is "
        "over for this session and your turn ends."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string", "minLength": 1, "maxLength": REASON_CHARS,
                "description": "Why you give the item back.",
            },
        },
        "required": ["reason"],
    },
)


def describe_item_completion(schema: dict[str, Any], contract: ItemContract) -> dict[str, Any]:
    """*schema*, ``complete_item``'s, specialized to the work item of *contract*.  Not mutated.

    A step or a vote takes its outcomes, its form with its file fields and a
    source for each field, as ``complete_step`` does.  A raised hand takes
    one of its choices, when it offers some, or an answer in words, a note,
    and a list of sources; none is required.
    """
    function = copy.deepcopy(schema["function"])
    parameters = function["parameters"]
    properties = parameters["properties"]
    if contract.kind != "raised_hand":
        parameters["properties"] = {name: properties[name] for name in _COMPLETION}
        parameters["required"] = list(_COMPLETION)
        return _specialise(
            {**schema, "function": function},
            outcomes=contract.outcomes or {}, form=contract.form or {}, files=contract.files,
        )
    answer = {name: properties[name] for name in _ANSWER}
    if contract.choices:
        answer["choice"]["enum"] = list(contract.choices)
    else:
        del answer["choice"]
    parameters["properties"] = {**answer, "sources": copy.deepcopy(_ANSWER_SOURCES)}
    parameters.pop("required", None)
    return {**schema, "function": function}


async def _stopped(
    client: KeelClient | None, store: Any, session_id: UUID, *, status: int | None = None, since: int | None = None,
) -> str:
    """Keel refused the item's report for good, or there is no Keel to report to: Keel is told the item failed,
    and its inputs leave the session."""
    if client is None:
        logger.warning("Session %s: no link to Keel; its work item stops for people to take", session_id)
        text = f"{ITEM_STOPPED} this runtime has no link to Keel; people take the item."
    else:
        logger.warning("Session %s: Keel refused its work item's report (%s); the item stops", session_id, status)
        await client.item_ended(session_id, status="failed", detail=f"keel-{status}")
        text = f"{ITEM_STOPPED} Keel refused this item's report ({status}); people take the item."
    await _redact(store, session_id)
    await _mark(store, session_id, _STOPPED, since=since)
    return text


async def _answered(
    answer: KeelAnswer, *, tool: str, client: KeelClient, store: Any, session_id: UUID, since: int | None,
) -> str:
    """What the model is told of Keel's *answer* to an item tool's report."""
    if answer.status == 200:
        text, mark = _RECORDED[tool]
        await _redact(store, session_id)
        await _mark(store, session_id, mark, since=since)
        return text
    if answer.status == 409 and answer.code == "not-running":
        return _NO_LONGER
    if answer.status == 409:
        return _rejection(answer, tool)
    if answer.transient:
        return _error(f"Keel could not be reached; call {tool} again.")
    return await _stopped(client, store, session_id, status=answer.status, since=since)


async def _report(tool: str, kwargs: dict[str, Any], fields: dict[str, Any], *, shorten: str) -> str:
    """Send the item's report, *fields* with the tool call's id, to *tool*'s route, and tell the model what came of it."""
    store = kwargs["session_store"]
    session_id = UUID(str(kwargs["session_id"]))
    client = keel_client()
    if client is None:
        result = await _stopped(None, store, session_id)
    else:
        since = await _began(store, session_id)
        answer = await _sent(
            client.session_call("POST", session_id, _ROUTES[tool], body={**fields, "call_id": kwargs["tool_call_id"]}),
            tool=tool, shorten=shorten,
        )
        if isinstance(answer, str):
            return answer
        result = await _answered(answer, tool=tool, client=client, store=store, session_id=session_id, since=since)
    if result.startswith((ITEM_ENDED, ITEM_STOPPED)):
        _remember_the_end(kwargs)
    return result


def _hand_answer(arguments: dict, contract: ItemContract) -> dict[str, Any] | str:
    """A raised hand's answer as Keel takes it, ``{"choice" | "answer", "note", "sources"}``; or what is wrong with it."""
    choice, answer = arguments.get("choice"), arguments.get("answer")
    if (choice is None) == (answer is None):
        return "Give exactly one of choice, one of the hand's choices, and answer, your answer in words."
    choices = contract.choices or []
    if choice is not None:
        if choice not in choices:
            if not choices:
                return "This raised hand offers no choices: give your answer in words."
            return f"choice must be one of: {', '.join(choices)}."
        given: dict[str, Any] = {"choice": choice}
    elif not isinstance(answer, str) or not answer.strip():
        return "answer must be your answer in words."
    else:
        given = {"answer": answer.strip()}
    note = arguments.get("note")
    if note is not None and not isinstance(note, str):
        return "note must be text."
    sources = arguments.get("sources")
    if sources is None:
        sources = []
    if not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources):
        return "sources must be a list of objects, each one place your answer comes from."
    return {**given, "note": note, "sources": sources}


async def handle_complete_item(arguments: dict, **kwargs: Any) -> str:
    contract = item_contract_in(kwargs.get("session_config"), session_id=kwargs.get("session_id"))
    if contract is None:
        return _error(_NOT_AN_ITEM)
    if _ended_in_this_wake(kwargs):
        return _NO_LONGER
    if contract.kind == "raised_hand":
        fields = _hand_answer(arguments, contract)
        if isinstance(fields, str):
            return _error(fields)
        if (refused := _call_id_refused(kwargs, "complete_item")) is not None:
            return _error(refused)
        return await _report("complete_item", kwargs, fields, shorten="the answer, its note and its sources")
    checked = await _completion(arguments, kwargs, contract, "complete_item")
    if isinstance(checked, str):
        return _error(checked)
    fields, _ = checked
    return await _report("complete_item", kwargs, fields, shorten="the output and its sources")


async def handle_return_item(arguments: dict, **kwargs: Any) -> str:
    if item_contract_in(kwargs.get("session_config"), session_id=kwargs.get("session_id")) is None:
        return _error(_NOT_AN_ITEM)
    if _ended_in_this_wake(kwargs):
        return _NO_LONGER
    reason = arguments.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= REASON_CHARS:
        return _error(f"reason must be 1 to {REASON_CHARS} characters.")
    if (refused := _call_id_refused(kwargs, "return_item")) is not None:
        return _error(refused)
    return await _report("return_item", kwargs, {"reason": reason.strip()}, shorten="the reason")


async def item_awaiting_report(store: Any, session: Any) -> Any | None:
    """*session*'s row as it is now, when it is a work item's own session whose item awaits its report: neither
    marked nor redacted (:func:`~surogates.tools.builtin.case_step.work_awaiting_report`).

    ``None`` otherwise, and when the row cannot be read, which is logged by
    session id.
    """
    return await work_awaiting_report(store, session, CASE_ITEM_KEY)


async def report_item_no_outcome(store: Any, session: Any, *, reason: str) -> None:
    """Tell Keel that a turn of a work item's session ended with no report ending the item.

    Keel gets ``no-outcome``, with *reason*, the turn end's own code, and
    gives the item back: once Keel took it, the item is marked ``returned``
    and its inputs leave the session.  Nothing is sent unless the item awaits
    its report (:func:`item_awaiting_report`).  Never raises: the turn ends
    whatever Keel answers.
    """
    if await item_awaiting_report(store, session) is None:
        return
    since = await _began(store, session.id)
    answer = await _tell_keel(
        session.id, lambda keel: keel.item_ended(session.id, status="no-outcome", detail=reason),
    )
    if answer is not None and answer.status == 200:
        await _redact(store, session.id)
        await _mark(store, session.id, "returned", since=since)


async def report_item_failed(store: Any, session: Any, *, code: str) -> None:
    """Tell Keel that a work item's session failed, and take the item's inputs out of it.

    Keel gets ``failed`` with *code*, the failure's own short code, never its
    text.  The item is over for the session whether or not Keel heard.  Both
    the end of a turn and the dispatcher report through here.  Nothing is
    sent, and nothing redacted, unless the item awaits its report
    (:func:`item_awaiting_report`).  Never raises.
    """
    if await item_awaiting_report(store, session) is None:
        return
    await _tell_keel(session.id, lambda keel: keel.item_ended(session.id, status="failed", detail=code))
    await _redact(store, session.id)


def register(registry: Any) -> None:
    registry.register(
        name="complete_item", schema=COMPLETE_ITEM_SCHEMA, handler=handle_complete_item, toolset="keel",
    )
    registry.register(
        name="return_item", schema=RETURN_ITEM_SCHEMA, handler=handle_return_item, toolset="keel",
    )
