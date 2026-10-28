"""``complete_step`` and ``raise_hand``: a step session reports its step to Keel.

Only the session Keel started for one step of a case is offered them.
``complete_step`` ends the step with one of its contract's outcomes, the
outputs that outcome requires and a source for each value; ``raise_hand``
asks a person when the session cannot finish.  Both go to Keel's door, and
Keel's answer decides what the session does next:

- Keel took the report, or the step is no longer this session's: the result
  starts with :data:`STEP_ENDED`, and the harness ends the turn.
- Keel rejected the report (a 409 with its own code): the result is the
  rejection, with the attempts Keel has left for it, and the model fixes its
  report and calls again, or raises a hand.
- Keel refused the report for good (401, 403, 404, 422, 409
  ``broken-definition``, 503 ``harness-off``, or any answer not named here),
  or this runtime has no link to Keel: Keel is told the step failed, the
  step's inputs leave the session, and the result starts with
  :data:`STEP_STOPPED`; the harness ends the turn, and people take the step.
- Keel could not be reached (no answer, a 408, a 429 or another 5xx): the
  model calls again.  A repeated call id gets Keel's first answer.

A report Keel could not read is never sent, and the model is told what to
fix: one larger than Keel takes, one holding NaN or an infinity, sources
that are not lists of objects, or a tool call id longer than Keel keeps.  Once a report ended the step, a later report in the same wake is
answered here, without Keel: the step is no longer the session's.  Not
once the wake's turn has read Keel's word to the step, which gives it back.

A report that ended the step for this round also marks the session's row
with what it came to (``REPORTED_KEY``), until Keel writes to the step
again.  A turn of the step that ends with no report ending the step is told
to Keel through :func:`report_no_outcome`; a session of the step that
fails, at its turn's end or in the dispatcher, through
:func:`report_step_failed`.  Neither tells Keel of a marked step, nor of
one that is over: a draft under review or a raised hand is never failed by
a crash, and its inputs stay for the person's word.

Each report carries what the step's session and its helpers have used so
far, and the bundle version of the session's latest wake
(:mod:`surogates.keel.run_facts`).  A tool's report cannot go without the use:
a store that cannot say fails the call, and the model calls again.  An end
goes without it instead, since Keel must hear of every end.

The harness checks only what Keel cannot, and what would make Keel refuse
the whole report: the outcome is one of the contract's, the output is an
object, the sources map fields to lists of objects, each file named is one
the case's files show, and the report is JSON within Keel's bounds.  A
raised hand's evidence is checked whole: each fact is the label, value and
optional source a person sees as a card, and its file one the case's files
show.  Keel validates the rest.  No log line here carries the case's data,
and an error repeats at most a path the model gave.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from surogates.keel.case_files import CaseFileError, case_path
from surogates.keel.client import BodyNotJSON, BodyTooLarge, KeelAnswer, KeelClient, keel_client
from surogates.keel.files import case_file_value, workspace_path
from surogates.keel.versions import VersionNotKept
from surogates.keel.items import ItemContract, is_case_item
from surogates.keel.run_facts import compile_id_in, wake_bundle_version, work_usage, work_usage_or_none
from surogates.keel.steps import (
    CASE_ITEM_KEY,
    CASE_STEP_KEY,
    REPORTED_KEY,
    STEP_ENDED,
    STEP_STOPPED,
    OutcomeContract,
    StepContract,
    is_case_step,
    step_contract_in,
)
from surogates.session.attachment_ingest import workspace_root_id
from surogates.session.events import EventType
from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

#: The most pages read one report carries.
MAX_PAGES_READ = 500
#: The longest knowledge base id and page path of a page read Keel takes.
KB_ID_CHARS = 200
PAGE_PATH_CHARS = 2000
#: The longest tool call id Keel keeps.
CALL_ID_CHARS = 200
#: The bounds of a raised hand.
QUESTION_CHARS = 2000
MAX_CHOICES = 20
CHOICE_CHARS = 200
MAX_EVIDENCE = 50
EVIDENCE_LABEL_CHARS = 200
EVIDENCE_VALUE_CHARS = 500

_NOT_A_STEP = "This session works no step of a case: there is no step to report."
#: How to name a file the case's files show, told when the model named another; it never repeats that path.
_NAME_A_CASE_FILE = "name one in /workspace, outside the platform's own folders, with no hidden file or folder in its path."
#: A page's digest, as Keel takes it: SHA-256, in lowercase hex.
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: What a report Keel took came to, as the step's mark; and the mark of a report Keel refused for good.
_RESULTS = frozenset({"completed", "drafted", "hand-raised"})
_STOPPED = "stopped"
_NO_LONGER = f"{STEP_ENDED} the step is no longer this session's."

#: The steps a report ended in a wake of this worker: each step session's id,
#: and the lease of the wake.  A later report in that wake is answered here, so
#: Keel never gets a second end, the inputs no second redaction, and the step
#: no attempt Keel counts.  A later wake holds a new lease, and reports again;
#: so does the wake whose turn reads Keel's word to the step (:func:`step_given_back`).
#: Only this local answer reads it: the row's report mark outlives the wake.
_ENDED_IN_WAKE: OrderedDict[str, str] = OrderedDict()
#: How many such steps a worker keeps, the oldest dropped first.
_ENDED_KEPT = 1024

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

# One fact behind a raised hand: what it is, what was found, and where.
_EVIDENCE_ITEM = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string", "minLength": 1, "maxLength": EVIDENCE_LABEL_CHARS,
            "description": "What the fact is, in a few words.",
        },
        "value": {
            "type": "string", "minLength": 1, "maxLength": EVIDENCE_VALUE_CHARS,
            "description": "What you found.",
        },
        "source": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": (
                        "A path from the case's files, as it is in /workspace: "
                        "never a case number, a field's name or a document's title."
                    ),
                },
                "page": {"type": "integer", "minimum": 1, "description": "The page of the file, from 1."},
            },
            "required": ["file"],
            "additionalProperties": False,
            "description": "Optional: the file you found it in, and the page.",
        },
    },
    "required": ["label", "value"],
    "additionalProperties": False,
}

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
                "type": "array", "maxItems": MAX_EVIDENCE, "items": _EVIDENCE_ITEM,
                "description": (
                    "Optional facts behind the question, each what you found and where. The "
                    "person sees each as a card that opens the file at that page."
                ),
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


def _specialise(
    schema: dict[str, Any], *, outcomes: Mapping[str, OutcomeContract], form: dict[str, Any], files: Mapping[str, str],
) -> dict[str, Any]:
    """*schema*, a completion's, specialized to a contract's *outcomes*, its *form* and its file fields (*files*):
    the outcomes it takes, the output's fields, a source for each, and what each outcome requires.  Not mutated."""
    function = copy.deepcopy(schema["function"])
    properties = function["parameters"]["properties"]
    properties["outcome"]["enum"] = list(outcomes)

    form = copy.deepcopy(form)
    fields = dict(form.get("properties") or {})
    for name, kind in files.items():
        fields[name] = _file_field(kind, fields.get(name))
    form["type"] = "object"
    form["properties"] = fields
    form.setdefault("description", properties["output"]["description"])
    properties["output"] = form

    sources = properties["sources"]
    sources["properties"] = {name: copy.deepcopy(_SOURCES) for name in fields}
    if form.get("additionalProperties") is False:
        sources["additionalProperties"] = False

    required = "\n".join(
        f"- {name}: " + (", ".join(outcome.required) if outcome.required else "no outputs")
        for name, outcome in outcomes.items()
    )
    function["description"] += f"\n\nThe outcomes, and the outputs each requires:\n{required}"
    return {**schema, "function": function}


def describe_completion(schema: dict[str, Any], contract: StepContract) -> dict[str, Any]:
    """*schema* specialized to *contract*: its outcomes, its form and file fields, and what each outcome requires.  Not mutated."""
    described = _specialise(schema, outcomes=contract.outcomes, form=contract.form, files=contract.files)
    if contract.mode == "draft":
        described["function"]["description"] += (
            "\n\nThis step is a draft: what you give is a proposal, which a person "
            "reviews and submits."
        )
    return described


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


async def _pages_read(store: Any, session: Any) -> list[dict[str, str | None]]:
    """The pages the case's sessions read, from the case root's log: each once for each compile that wrote the
    text read, sorted by knowledge base id, path, digest and compile, the first :data:`MAX_PAGES_READ` of them.

    An entry always carries ``compile_id``: the compile that last wrote the
    text of the page read, None when the read did not name one (an event
    written before reads named their compile, or anything that is not a
    compile id).  One page can so be listed under more than one compile.

    A page whose digest is not a SHA-256 hex digest, or whose knowledge base
    id or path is empty or longer than Keel takes, is left out: Keel refuses
    the whole report for one.
    """
    events = await store.get_events(UUID(workspace_root_id(session)), types=[EventType.KB_PAGE_READ])
    pages = {
        (data["kb_id"], data["path"], data["sha256"], compile_id_in(data.get("compile_id")))
        for data in (event.data for event in events)
        if isinstance(data, dict)
        and all(isinstance(data.get(key), str) for key in ("kb_id", "path", "sha256"))
        and _SHA256.fullmatch(data["sha256"])
        and 0 < len(data["kb_id"]) <= KB_ID_CHARS
        and 0 < len(data["path"]) <= PAGE_PATH_CHARS
    }
    return [
        {"kb_id": kb_id, "path": path, "sha256": sha256, "compile_id": compile_id}
        for kb_id, path, sha256, compile_id in sorted(pages, key=lambda page: (*page[:3], page[3] or ""))[
            :MAX_PAGES_READ
        ]
    ]


async def _redact(store: Any, session_id: UUID) -> None:
    """The inputs of the step or the work item leave its session: it is over for the session."""
    try:
        await store.redact_case_inputs(session_id)
    except Exception as exc:
        logger.warning("Session %s: its case inputs were not redacted (%s)", session_id, type(exc).__name__)


async def _mark(store: Any, session_id: UUID, result: str, *, since: int | None) -> None:
    """A report ended the step or the work item for this round, with *result*: the session's row says so until
    Keel writes to it.

    *since* is the session's last event id when the report began: a message
    of Keel's written after it leaves the step unmarked.
    """
    try:
        await store.mark_case_step_reported(session_id, result, since=since)
    except Exception as exc:
        logger.warning("Session %s: its report was not marked (%s)", session_id, type(exc).__name__)


async def _began(store: Any, session_id: UUID) -> int | None:
    """The session's last event id, read as a report to Keel begins; ``None``, logged, when it cannot be read."""
    try:
        return await store.last_event_id(session_id)
    except Exception as exc:
        logger.warning("Session %s: its last event was not read (%s)", session_id, type(exc).__name__)
        return None


async def _stopped(
    client: KeelClient | None, store: Any, session_id: UUID, usage: dict[str, Any] | None, *,
    bundle_version: str | None = None, status: int | None = None, since: int | None = None,
) -> str:
    """Keel refused the step's report for good, or there is no Keel to report to: Keel is told the step
    failed, with the *usage* and the *bundle_version* the refused report carried, and its inputs leave the session.
    With no Keel nothing is told, so none is read: *usage* is ``None``."""
    if client is None:
        logger.warning("Session %s: no link to Keel; its step stops for people to take", session_id)
        text = f"{STEP_STOPPED} this runtime has no link to Keel; people take the step."
    else:
        logger.warning("Session %s: Keel refused its step's report (%s); the step stops", session_id, status)
        await client.ended(
            session_id, reason="failed", detail=f"keel-{status}", usage=usage, bundle_version=bundle_version,
        )
        text = f"{STEP_STOPPED} Keel refused this step's report ({status}); people take the step."
    await _redact(store, session_id)
    await _mark(store, session_id, _STOPPED, since=since)
    return text


#: A completion's way out, named beside the attempts Keel has left for it: the tool that hands the work to a person.
_WAY_OUT = {"complete_step": "raise_hand", "complete_item": "return_item"}


def _rejection(answer: KeelAnswer, tool: str) -> str:
    """Keel's rejection of a report, with the attempts it has left for the step or the work item when it says."""
    message = answer.message or "Keel rejected the report."
    left = answer.data.get("attempts_left") if isinstance(answer.data, dict) else None
    if isinstance(left, int) and not isinstance(left, bool) and left >= 0:
        attempts = f"{left} attempt{'' if left == 1 else 's'} left"
        if tool in _WAY_OUT:
            attempts += f"; fix the report and call {tool} again, or call {_WAY_OUT[tool]}"
        message += f" ({attempts})"
    return _error(message, code=answer.code)


async def _answered(
    answer: KeelAnswer, *, tool: str, client: KeelClient, store: Any, session_id: UUID, usage: dict[str, Any] | None,
    bundle_version: str | None, since: int | None,
) -> str:
    """What the model is told of Keel's *answer* to a step tool's report."""
    if answer.status == 200:
        result = answer.data.get("result") if isinstance(answer.data, dict) else None
        if result == "completed":
            await _redact(store, session_id)
        if result not in _RESULTS:
            # Keel took it without saying how: as the tool's own report would end the step.
            result = "completed" if tool == "complete_step" else "hand-raised"
        await _mark(store, session_id, result, since=since)
        return f"{STEP_ENDED} {answer.message or 'Keel took the report.'}"
    if answer.status == 409 and answer.code == "not-running":
        return _NO_LONGER
    if answer.status == 409 and answer.code != "broken-definition":
        return _rejection(answer, tool)
    if answer.transient:
        return _error(f"Keel could not be reached; call {tool} again.")
    return await _stopped(
        client, store, session_id, usage, bundle_version=bundle_version, status=answer.status, since=since,
    )


def _ended_in_this_wake(kwargs: dict[str, Any]) -> bool:
    lease = kwargs.get("lease_token")
    return lease is not None and _ENDED_IN_WAKE.get(str(kwargs.get("session_id"))) == str(lease)


def step_given_back(session_id: object) -> None:
    """Keel's word, an answer or a revision, gave the step back to session *session_id*, and the turn
    under way reads it: a report the turn makes from here on reaches Keel."""
    _ENDED_IN_WAKE.pop(str(session_id), None)


def _remember_the_end(kwargs: dict[str, Any]) -> None:
    lease = kwargs.get("lease_token")
    if lease is None:
        return
    session_id = str(kwargs.get("session_id"))
    _ENDED_IN_WAKE[session_id] = str(lease)
    _ENDED_IN_WAKE.move_to_end(session_id)
    while len(_ENDED_IN_WAKE) > _ENDED_KEPT:
        _ENDED_IN_WAKE.popitem(last=False)


async def _sent(
    report: Awaitable[KeelAnswer], *, tool: str, shorten: str, noun: str = "report",
) -> KeelAnswer | str:
    """Keel's answer to *report*, a call of *tool* sending its *noun*; or, when it was never sent, the error the
    model reads: what to fix (*shorten*, for one larger than Keel takes) before calling again."""
    try:
        return await report
    except BodyTooLarge as exc:
        return _error(
            f"The {noun} is {exc.size:,} bytes, over the {exc.limit:,} bytes Keel takes. "
            f"Shorten {shorten} and call {tool} again.",
        )
    except BodyNotJSON:
        return _error(
            f"The {noun} holds a number JSON cannot carry (NaN or Infinity). Give a real "
            f"number, or leave the value out, and call {tool} again.",
        )


async def _send_report(
    tool: str, kwargs: dict[str, Any], fields: dict[str, Any], *, store: Any, session_id: UUID, session: Any, shorten: str,
) -> str:
    """Send the step's report, *fields* with its call id, pages read, usage and bundle version, and tell the model
    what came of it.

    The usage is the work's, its helpers' included (:func:`work_usage`).  A report cannot be sent without it: a
    store that cannot read it fails the call, as one that cannot read the pages does, and the model calls again.
    A runtime with no link to Keel tells no one, so it reads none of it.
    """
    client = keel_client()
    if client is None:
        result = await _stopped(None, store, session_id, None)
    else:
        send = client.complete if tool == "complete_step" else client.raise_hand
        usage = await work_usage(store, session_id)
        bundle_version = await wake_bundle_version(store, session_id)
        body = {
            "call_id": kwargs["tool_call_id"], **fields,
            "pages_read": await _pages_read(store, session), "usage": usage, "bundle_version": bundle_version,
        }
        since = await _began(store, session_id)
        answer = await _sent(send(session_id, body), tool=tool, shorten=shorten)
        if isinstance(answer, str):
            return answer
        result = await _answered(
            answer, tool=tool, client=client, store=store, session_id=session_id, usage=usage,
            bundle_version=bundle_version, since=since,
        )
    if result.startswith((STEP_ENDED, STEP_STOPPED)):
        _remember_the_end(kwargs)
    return result


#: Whether a session is the own session of the work its config holds under each key.
_OWN_SESSION = {CASE_STEP_KEY: is_case_step, CASE_ITEM_KEY: is_case_item}


async def work_awaiting_report(store: Any, session: Any, key: str) -> Any | None:
    """*session*'s row as it is now, when it is the own session of the work its config holds under *key*, a
    step (:data:`CASE_STEP_KEY`) or a work item (:data:`CASE_ITEM_KEY`), and that work awaits its report.

    ``None`` for any other session, and when Keel knows how the work stands:
    a report ended it for this round (the contract's ``REPORTED_KEY`` mark,
    which Keel's next message to the step clears), or it is over (its inputs
    were redacted).  ``None`` too when the row cannot be read, which is
    logged by session id.  Read afresh, not from the wake's copy: a report
    of this wake, or of an earlier one, marked it after the wake began.
    """
    own = _OWN_SESSION[key]
    if not own(session):
        return None
    try:
        row = await store.get_session(session.id)
    except Exception as exc:
        logger.warning("Session %s: its case work was not read (%s)", session.id, type(exc).__name__)
        return None
    if not own(row):
        return None
    contract = row.config[key]
    if contract.get(REPORTED_KEY) or contract.get("inputs_redacted") is True:
        return None
    return row


async def step_awaiting_report(store: Any, session: Any) -> Any | None:
    """*session*'s row as it is now, when it is a step's own session whose step awaits its report
    (:func:`work_awaiting_report`)."""
    return await work_awaiting_report(store, session, CASE_STEP_KEY)


async def _tell_keel(session_id: Any, end: Callable[[KeelClient], Awaitable[KeelAnswer]]) -> KeelAnswer | None:
    """Tell Keel how session *session_id*'s case work ended, by *end*, the client's call that says it, and return
    Keel's answer; log by session id what was not sent.

    Never raises: ``None`` when nothing was sent.
    """
    try:
        client = keel_client()
        if client is None:
            logger.warning("Session %s: no link to Keel; Keel is not told how its case work ended", session_id)
            return None
        return await end(client)
    except Exception as exc:
        logger.warning("Session %s: Keel was not told how its case work ended (%s)", session_id, type(exc).__name__)
        return None


async def report_no_outcome(store: Any, session: Any, *, reason: str) -> None:
    """Tell Keel that a turn of a step's session ended with no report ending the step.

    Keel gets ``no-outcome``, with *reason*, the turn end's own code, the
    work's usage when the store can say and the bundle version of the step's
    latest wake, and decides what comes of the step; the inputs stay.
    When Keel takes it as a raised hand, a person answers in the session, and
    the step is marked so, as a raised hand of the model's is.  Nothing is
    sent unless the step awaits its report (:func:`step_awaiting_report`).
    Never raises: the turn ends whatever Keel answers.
    """
    row = await step_awaiting_report(store, session)
    if row is None:
        return
    since = await _began(store, session.id)
    usage = await work_usage_or_none(store, session.id)
    bundle_version = await wake_bundle_version(store, session.id)
    answer = await _tell_keel(
        session.id,
        lambda keel: keel.ended(
            session.id, reason="no-outcome", detail=reason, usage=usage, bundle_version=bundle_version,
        ),
    )
    if answer is not None and answer.status == 200 and isinstance(answer.data, dict) and (
        answer.data.get("result") == "hand-raised"
    ):
        await _mark(store, session.id, "hand-raised", since=since)


async def report_step_failed(store: Any, session: Any, *, code: str) -> None:
    """Tell Keel that a step's session failed, and take the step's inputs out of it.

    Keel gets ``failed`` with *code*, the failure's own short code, never its
    text, the work's usage when the store can say and the bundle version of
    the step's latest wake.  The step is over for the session whether or not
    Keel heard.  Both the end of a turn and the dispatcher, which fails a
    session whose wakes kept crashing, report through here.  Nothing is sent,
    and nothing redacted, unless the step awaits its report
    (:func:`step_awaiting_report`): a draft under review or a raised hand
    keeps its inputs for the person's word.  Never raises.
    """
    if await step_awaiting_report(store, session) is None:
        return
    usage = await work_usage_or_none(store, session.id)
    bundle_version = await wake_bundle_version(store, session.id)
    await _tell_keel(
        session.id,
        lambda keel: keel.ended(session.id, reason="failed", detail=code, usage=usage, bundle_version=bundle_version),
    )
    await _redact(store, session.id)


def _call_id_refused(kwargs: dict[str, Any], tool: str) -> str | None:
    """What is wrong with the tool call's id as Keel keeps it, or ``None``."""
    call_id = kwargs.get("tool_call_id")
    if not call_id:
        return f"{tool} needs its tool call's id."
    if len(str(call_id)) > CALL_ID_CHARS:
        return (
            f"This tool call's id is longer than the {CALL_ID_CHARS} characters Keel takes, "
            "so the report was not sent."
        )
    return None


def _sources_refused(sources: Any) -> str | None:
    """What is wrong with *sources*, or ``None``: each field's sources must be a list of objects."""
    if not isinstance(sources, dict):
        return "sources must be an object of field to a list of sources."
    for name, given in sources.items():
        if not isinstance(given, list) or not all(isinstance(source, dict) for source in given):
            return f"sources.{name} must be a list of objects, each one place its value comes from."
    return None


async def _step_session(kwargs: dict[str, Any]) -> tuple[Any, UUID, Any]:
    """The step session's store, id and row."""
    store = kwargs["session_store"]
    session_id = UUID(str(kwargs["session_id"]))
    return store, session_id, await store.get_session(session_id)


async def _files(
    output: dict[str, Any], contract: StepContract | ItemContract, *, storage: Any, session: Any,
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
        for index, path in enumerate(paths):
            try:
                files.append(await case_file_value(storage, session, contract.case_id, path))
            except CaseFileError:
                at = f"{name}[{index}]" if kind == "list" else name
                return f"{at} is not a file of the case: {_NAME_A_CASE_FILE}"
            except VersionNotKept as exc:
                # The store's own error can quote the file's key; its class says enough.
                logger.warning("Session %s: a file's version was not kept (%s)", session.id, type(exc.__cause__).__name__)
                return f"{name}: the file could not be kept for Keel just now; call again."
            except ValueError:
                return f"{name}: {path} is not a path in /workspace."
            except FileNotFoundError:
                return f"{name}: no file {path} in the workspace"
        filled[name] = files if kind == "list" else files[0]
    return filled


async def _completion(
    arguments: dict, kwargs: dict[str, Any], contract: StepContract | ItemContract, tool: str,
) -> tuple[dict[str, Any], Any] | str:
    """The fields of *tool*'s completion of *contract*, as Keel takes them, and the session's row; or what is
    wrong with them, and nothing is sent.

    The outcome must be one of the contract's, the output an object, the
    sources a list of objects for each field, and the call id one Keel
    keeps; each file field's paths become the case's files.
    """
    outcomes = contract.outcomes or {}
    outcome = arguments.get("outcome")
    if not isinstance(outcome, str) or outcome not in outcomes:
        return f"outcome must be one of: {', '.join(outcomes)}."
    output = arguments.get("output", {})
    if not isinstance(output, dict):
        return "output must be an object of field to value."
    sources = arguments.get("sources", {})
    if (refused := _sources_refused(sources)) is not None:
        return refused
    if (refused := _call_id_refused(kwargs, tool)) is not None:
        return refused

    _, _, session = await _step_session(kwargs)
    filled = await _files(output, contract, storage=kwargs.get("storage"), session=session)
    if isinstance(filled, str):
        return filled
    return {"outcome": outcome, "output": filled, "sources": sources}, session


async def handle_complete_step(arguments: dict, **kwargs: Any) -> str:
    contract = step_contract_in(kwargs.get("session_config"), session_id=kwargs.get("session_id"))
    if contract is None:
        return _error(_NOT_A_STEP)
    if _ended_in_this_wake(kwargs):
        return _NO_LONGER
    checked = await _completion(arguments, kwargs, contract, "complete_step")
    if isinstance(checked, str):
        return _error(checked)
    fields, session = checked
    return await _send_report(
        "complete_step", kwargs, fields,
        store=kwargs["session_store"], session_id=session.id, session=session, shorten="the output and its sources",
    )


def _fact(index: int, item: Any) -> dict[str, Any] | str:
    """*item*, the fact at *index* in a raised hand's evidence, as Keel takes it; or what is wrong with it.

    Its file is named as the case's files name it: relative to the case's
    workspace, and never one their listing leaves out.  An error names the
    fact by its index, never by the path it gave.
    """
    # Lenient where a model slips: a label's and a value's length is counted once stripped, and a null source or
    # page is read as absent.
    at = f"evidence[{index}]"
    if not isinstance(item, dict) or not set(item) <= {"label", "value", "source"}:
        return f"{at} must be an object of a label, a value and an optional source, and nothing else."
    fact: dict[str, Any] = {}
    for key, limit in (("label", EVIDENCE_LABEL_CHARS), ("value", EVIDENCE_VALUE_CHARS)):
        text = item.get(key)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= limit:
            return f"{at}.{key} must be 1 to {limit} characters."
        fact[key] = text.strip()
    source = item.get("source")
    if source is None:
        return fact
    if not isinstance(source, dict) or not set(source) <= {"file", "page"} or not isinstance(source.get("file"), str):
        return f"{at}.source must be an object of a file in /workspace and an optional page, and nothing else."
    page = source.get("page")
    if page is not None and (not isinstance(page, int) or isinstance(page, bool) or page < 1):
        return f"{at}.source.page must be a whole number from 1."
    relative = workspace_path(source["file"])
    if relative is None:
        return f"{at}.source.file is not a path in /workspace."
    try:
        file = case_path(relative)
    except CaseFileError:
        return f"{at}.source.file is not a file of the case: {_NAME_A_CASE_FILE}"
    fact["source"] = {"file": file} if page is None else {"file": file, "page": page}
    return fact


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
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        return f"evidence must be a list of at most {MAX_EVIDENCE} facts."
    facts = []
    for index, item in enumerate(evidence):
        fact = _fact(index, item)
        if isinstance(fact, str):
            return fact
        facts.append(fact)
    return question.strip(), choices, facts


async def handle_raise_hand(arguments: dict, **kwargs: Any) -> str:
    contract = step_contract_in(kwargs.get("session_config"), session_id=kwargs.get("session_id"))
    if contract is None:
        return _error(_NOT_A_STEP)
    if _ended_in_this_wake(kwargs):
        return _NO_LONGER
    hand = _hand(arguments)
    if isinstance(hand, str):
        return _error(hand)
    question, choices, evidence = hand
    if (refused := _call_id_refused(kwargs, "raise_hand")) is not None:
        return _error(refused)

    store, session_id, session = await _step_session(kwargs)
    return await _send_report(
        "raise_hand", kwargs, {"question": question, "choices": choices, "evidence": evidence},
        store=store, session_id=session_id, session=session, shorten="the question and its evidence",
    )


def register(registry: Any) -> None:
    registry.register(
        name="complete_step", schema=COMPLETE_STEP_SCHEMA, handler=handle_complete_step, toolset="keel",
    )
    registry.register(
        name="raise_hand", schema=RAISE_HAND_SCHEMA, handler=handle_raise_hand, toolset="keel",
    )
