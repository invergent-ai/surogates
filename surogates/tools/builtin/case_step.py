"""``complete_step`` and ``raise_hand``: a step session reports its step to Keel.

Only the session Keel started for one step of a case is offered them.
``complete_step`` ends the step with one of its contract's outcomes, the
outputs that outcome requires and a source for each value; ``raise_hand``
asks a person when the session cannot finish.  Both go to Keel's door, and
Keel's answer decides what the session does next:

- Keel took the report, or the step is no longer this session's: the result
  starts with :data:`STEP_ENDED`, and the harness ends the turn.
- Keel rejected the report (a 409 with its own code): the result is the
  rejection, and the model fixes its report and calls again.
- Keel refused the report for good (401, 403, 404, 422, 409
  ``broken-definition``, 503 ``harness-off``, or any answer not named here),
  or this runtime has no link to Keel: Keel is told the step failed, the
  step's inputs leave the session, and the result starts with
  :data:`STEP_STOPPED`; the harness ends the turn, and people take the step.
- Keel could not be reached (no answer, a 408, a 429 or another 5xx): the
  model calls again.  A repeated call id gets Keel's first answer.

The harness checks only what Keel cannot: the outcome is one of the
contract's, the output and sources are objects, and each file named is in
the case's workspace.  Keel validates the rest.  No log line here carries
the case's data, and an error repeats at most a path the model gave.
"""

from __future__ import annotations

import copy
import json
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from surogates.keel.client import KeelAnswer, KeelClient, keel_client
from surogates.keel.files import case_file_value
from surogates.keel.steps import STEP_ENDED, STEP_STOPPED, StepContract, step_contract_in
from surogates.session.attachment_ingest import workspace_root_id
from surogates.session.events import EventType
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

#: The most pages read one report carries.
MAX_PAGES_READ = 500
#: The bounds of a raised hand.
QUESTION_CHARS = 2000
MAX_CHOICES = 20
CHOICE_CHARS = 200
MAX_EVIDENCE = 50

_NOT_A_STEP = "This session works no step of a case: there is no step to report."

# A non-empty list of where one output's value comes from.
_SOURCES = {"type": "array", "minItems": 1, "items": {"type": "object"}}

COMPLETE_STEP_SCHEMA = ToolSchema(
    name="complete_step",
    description=(
        "End this session's step of the case with one of its outcomes. Give, in "
        "'output', every output the outcome requires, and in 'sources' where each "
        "value comes from. A file field takes the path of a file in /workspace, or "
        "a list of paths. Keel checks the result: a refusal comes back as an error "
        "to fix before calling again. Once Keel takes it, the step is over for this "
        "session and your turn ends."
    ),
    parameters={
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "description": "The outcome the step ends with."},
            "output": {"type": "object", "description": "The step's outputs, by field."},
            "sources": {
                "type": "object",
                "description": (
                    "For each output field, a non-empty list of where its value comes "
                    "from, each an object: a knowledge page as {\"kb_id\", \"path\"}, a "
                    "file in /workspace as {\"file\"}, one of the step's inputs as "
                    "{\"input\"}, or a web page as {\"url\"}."
                ),
                "additionalProperties": _SOURCES,
            },
        },
        "required": ["outcome", "output", "sources"],
    },
)

RAISE_HAND_SCHEMA = ToolSchema(
    name="raise_hand",
    description=(
        "Ask a person about this session's step of the case when you cannot finish "
        "it: what you need decided or told, the answers you see, and what you "
        "found. Your turn ends; the person's answer comes back as a message in "
        "this session, and you go on with the step then."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string", "minLength": 1, "maxLength": QUESTION_CHARS,
                "description": "What you need a person to decide or tell you.",
            },
            "choices": {
                "type": "array", "maxItems": MAX_CHOICES,
                "items": {"type": "string", "maxLength": CHOICE_CHARS},
                "description": "Optional answers the person can pick from.",
            },
            "evidence": {
                "type": "array", "maxItems": MAX_EVIDENCE, "items": {"type": "object"},
                "description": "Optional facts behind the question, each an object: where you looked and what you found.",
            },
        },
        "required": ["question"],
    },
)


def _file_field(kind: str, declared: Any) -> dict[str, Any]:
    """The schema the model fills a file field with: a path in the workspace, or a list of them."""
    note = declared.get("description") if isinstance(declared, dict) else None
    if kind == "list":
        field: dict[str, Any] = {
            "type": "array", "items": {"type": "string"}, "description": "Paths of files in /workspace.",
        }
    else:
        field = {"type": "string", "description": "The path of a file in /workspace."}
    if note:
        field["description"] += f" {note}"
    return field


def describe_completion(schema: dict[str, Any], contract: StepContract) -> dict[str, Any]:
    """*schema* specialized to *contract*: its outcomes, its form and file fields, and what each outcome requires.  Not mutated."""
    function = copy.deepcopy(schema["function"])
    properties = function["parameters"]["properties"]
    properties["outcome"]["enum"] = list(contract.outcomes)

    form = copy.deepcopy(contract.form)
    fields = dict(form.get("properties") or {})
    for name, kind in contract.files.items():
        fields[name] = _file_field(kind, fields.get(name))
    form["type"] = "object"
    form["properties"] = fields
    form.setdefault("description", properties["output"]["description"])
    properties["output"] = form

    sources = properties["sources"]
    sources["properties"] = {name: copy.deepcopy(_SOURCES) for name in fields}
    if form.get("additionalProperties") is False:
        sources["additionalProperties"] = False

    outcomes = "\n".join(
        f"- {name}: " + (", ".join(outcome.required) if outcome.required else "no outputs")
        for name, outcome in contract.outcomes.items()
    )
    function["description"] += f"\n\nThe outcomes, and the outputs each requires:\n{outcomes}"
    if contract.mode == "draft":
        function["description"] += (
            "\n\nThis step is a draft: what you give is a proposal, which a person "
            "reviews and submits."
        )
    return {**schema, "function": function}


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


def _usage(session: Any) -> dict[str, Any]:
    """What the step's session has used so far, as Keel takes it."""
    return {
        "input_tokens": int(session.input_tokens or 0),
        "output_tokens": int(session.output_tokens or 0),
        "estimated_cost_usd": format(Decimal(session.estimated_cost_usd or 0), "f"),
    }


async def _pages_read(store: Any, session: Any) -> list[dict[str, str]]:
    """The pages the case's sessions read, from the case root's log: each once, in order, at most :data:`MAX_PAGES_READ`."""
    events = await store.get_events(UUID(workspace_root_id(session)), types=[EventType.KB_PAGE_READ])
    pages = {
        (data["kb_id"], data["path"], data["sha256"])
        for data in (event.data for event in events)
        if isinstance(data, dict) and all(isinstance(data.get(key), str) for key in ("kb_id", "path", "sha256"))
    }
    return [
        {"kb_id": kb_id, "path": path, "sha256": sha256}
        for kb_id, path, sha256 in sorted(pages)[:MAX_PAGES_READ]
    ]


async def _redact(store: Any, session_id: UUID) -> None:
    """The step's inputs leave its session: the step is over for it."""
    try:
        await store.redact_case_inputs(session_id)
    except Exception as exc:
        logger.warning("Session %s: the step's inputs were not redacted (%s)", session_id, type(exc).__name__)


def _transient(answer: KeelAnswer) -> bool:
    """Whether Keel may take the same report later: it was not reached, or was busy or failing."""
    status = answer.status
    if status is None or status in (408, 429):
        return True
    return status >= 500 and not (status == 503 and answer.code == "harness-off")


async def _stopped(
    client: KeelClient | None, store: Any, session_id: UUID, usage: dict[str, Any], *, status: int | None = None,
) -> str:
    """Keel refused the step's report for good, or there is no Keel to report to: Keel is told the step
    failed, and its inputs leave the session."""
    if client is None:
        logger.warning("Session %s: no link to Keel; its step stops for people to take", session_id)
        text = f"{STEP_STOPPED} this runtime has no link to Keel; people take the step."
    else:
        logger.warning("Session %s: Keel refused its step's report (%s); the step stops", session_id, status)
        await client.ended(session_id, reason="failed", detail=f"keel-{status}", usage=usage)
        text = f"{STEP_STOPPED} Keel refused this step's report ({status}); people take the step."
    await _redact(store, session_id)
    return text


async def _answered(
    answer: KeelAnswer, *, tool: str, client: KeelClient, store: Any, session_id: UUID, usage: dict[str, Any],
) -> str:
    """What the model is told of Keel's *answer* to a step tool's report."""
    if answer.status == 200:
        if isinstance(answer.data, dict) and answer.data.get("result") == "completed":
            await _redact(store, session_id)
        return f"{STEP_ENDED} {answer.message or 'Keel took the report.'}"
    if answer.status == 409 and answer.code == "not-running":
        return f"{STEP_ENDED} the step is no longer this session's."
    if answer.status == 409 and answer.code != "broken-definition":
        return _error(answer.message or "Keel rejected the report.", code=answer.code)
    if _transient(answer):
        return _error(f"Keel could not be reached; call {tool} again.")
    return await _stopped(client, store, session_id, usage, status=answer.status)


async def _step_session(kwargs: dict[str, Any]) -> tuple[Any, UUID, Any]:
    """The step session's store, id and row."""
    store = kwargs["session_store"]
    session_id = UUID(str(kwargs["session_id"]))
    return store, session_id, await store.get_session(session_id)


async def _files(
    output: dict[str, Any], contract: StepContract, *, storage: Any, session: Any,
) -> dict[str, Any] | str:
    """*output* with each file field's paths turned into Keel's file values, or the error for a field."""
    filled = dict(output)
    for name, kind in contract.files.items():
        if name not in output:
            continue
        value = output[name]
        if kind == "list":
            if not isinstance(value, list) or not all(isinstance(path, str) for path in value):
                return f"{name}: give a list of paths of files in /workspace."
            paths = value
        else:
            if not isinstance(value, str):
                return f"{name}: give the path of one file in /workspace."
            paths = [value]
        files = []
        for path in paths:
            try:
                files.append(await case_file_value(storage, session, contract.case_id, path))
            except ValueError:
                return f"{name}: {path} is not a path in /workspace."
            except FileNotFoundError:
                return f"{name}: no file {path} in the workspace"
        filled[name] = files if kind == "list" else files[0]
    return filled


async def handle_complete_step(arguments: dict, **kwargs: Any) -> str:
    contract = step_contract_in(kwargs.get("session_config"), session_id=kwargs.get("session_id"))
    if contract is None:
        return _error(_NOT_A_STEP)
    outcome = arguments.get("outcome")
    if not isinstance(outcome, str) or outcome not in contract.outcomes:
        return _error(f"outcome must be one of: {', '.join(contract.outcomes)}.")
    output = arguments.get("output", {})
    if not isinstance(output, dict):
        return _error("output must be an object of field to value.")
    sources = arguments.get("sources", {})
    if not isinstance(sources, dict):
        return _error("sources must be an object of field to a list of sources.")
    call_id = kwargs.get("tool_call_id")
    if not call_id:
        return _error("complete_step needs its tool call's id.")

    store, session_id, session = await _step_session(kwargs)
    filled = await _files(output, contract, storage=kwargs.get("storage"), session=session)
    if isinstance(filled, str):
        return _error(filled)
    usage = _usage(session)
    client = keel_client()
    if client is None:
        return await _stopped(None, store, session_id, usage)
    answer = await client.complete(session_id, {
        "call_id": call_id,
        "outcome": outcome,
        "output": filled,
        "sources": sources,
        "pages_read": await _pages_read(store, session),
        "usage": usage,
    })
    return await _answered(
        answer, tool="complete_step", client=client, store=store, session_id=session_id, usage=usage,
    )


def _hand(arguments: dict) -> tuple[str, list[str], list[dict]] | str:
    """The question, choices and evidence of a raised hand, or what is wrong with them."""
    question = arguments.get("question")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= QUESTION_CHARS:
        return f"question must be 1 to {QUESTION_CHARS} characters."
    choices = arguments.get("choices") or []
    if (
        not isinstance(choices, list) or len(choices) > MAX_CHOICES
        or not all(isinstance(choice, str) and len(choice) <= CHOICE_CHARS for choice in choices)
    ):
        return f"choices must be at most {MAX_CHOICES} answers of at most {CHOICE_CHARS} characters."
    evidence = arguments.get("evidence") or []
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE or not all(isinstance(e, dict) for e in evidence):
        return f"evidence must be at most {MAX_EVIDENCE} objects."
    return question.strip(), choices, evidence


async def handle_raise_hand(arguments: dict, **kwargs: Any) -> str:
    contract = step_contract_in(kwargs.get("session_config"), session_id=kwargs.get("session_id"))
    if contract is None:
        return _error(_NOT_A_STEP)
    hand = _hand(arguments)
    if isinstance(hand, str):
        return _error(hand)
    question, choices, evidence = hand
    call_id = kwargs.get("tool_call_id")
    if not call_id:
        return _error("raise_hand needs its tool call's id.")

    store, session_id, session = await _step_session(kwargs)
    usage = _usage(session)
    client = keel_client()
    if client is None:
        return await _stopped(None, store, session_id, usage)
    answer = await client.raise_hand(session_id, {
        "call_id": call_id,
        "question": question,
        "choices": choices,
        "evidence": evidence,
        "pages_read": await _pages_read(store, session),
        "usage": usage,
    })
    return await _answered(
        answer, tool="raise_hand", client=client, store=store, session_id=session_id, usage=usage,
    )


def register(registry: Any) -> None:
    registry.register(
        name="complete_step", schema=COMPLETE_STEP_SCHEMA, handler=handle_complete_step, toolset="keel",
    )
    registry.register(
        name="raise_hand", schema=RAISE_HAND_SCHEMA, handler=handle_raise_hand, toolset="keel",
    )
