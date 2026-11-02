"""The ``keel_process`` tools: a person's case agent reads a process instance, and drafts into the person's form,
through Keel.

Only a case agent's own session on a process instance is offered them
(:func:`surogates.keel.steps.gate_case_tools`).  The agent never reads the
instance's workspace: each tool asks Keel's door, under the session's id, and
Keel answers as the person, logging each read as a view through the case
agent.  Every item Keel gives carries a ``cite`` label the agent cites.

- ``process_read``, ``process_history``, ``process_waits`` and
  ``process_move_check`` read the instance, its history, what it waits for and
  whether it can move to its process's newest version, and answer Keel's JSON
  as it came (:func:`surogates.tools.builtin.keel_case.answered`).  The move
  check moves nothing.
- ``process_draft_form`` drafts values into the person's form on an open task
  the person holds, each with its sources.  It drafts values only: the
  outcome is the person's to choose, so a call naming one sends nothing.

None of them completes a task, retries, cancels, moves the instance, or sends
a message or a signal.  No log line here carries the instance's data.
"""

from __future__ import annotations

from typing import Any

from surogates.keel.case_agents import is_process_agent_config
from surogates.keel.client import KeelClient, keel_client
from surogates.tools.builtin.case_step import _error, _sent
from surogates.tools.builtin.keel_case import _NO_LINK, _RESULT_CHARS, answered
from surogates.tools.registry import ToolSchema

_NOT_A_PROCESS_AGENT = (
    "This session is no person's case agent on a process instance: it has no instance to read through Keel."
)
_OUTCOME = "process_draft_form drafts values only: the outcome is the person's to choose."
#: How each tool that only reads names the door's route under the session.
_READS = {
    "process_read": "process",
    "process_history": "process/history",
    "process_waits": "process/waits",
    "process_move_check": "process/move",
}

_THROUGH_KEEL = "It reads the instance as the person you help, through Keel."
_CITE = "Each item carries a 'cite' label: cite it when you use what it says."

PROCESS_READ_SCHEMA = ToolSchema(
    name="process_read",
    description=(
        "Read the process instance you work on: its number, process, version and status, its data field by "
        "field, its open tasks with who holds each and the form fields of those the person holds, and its "
        f"incidents with why each was recorded. {_THROUGH_KEEL} {_CITE}"
    ),
    parameters={"type": "object", "properties": {}},
)

PROCESS_HISTORY_SCHEMA = ToolSchema(
    name="process_history",
    description=(
        "Read the process instance's history: what happened on it, in order, each entry a sentence, with who "
        f"drafted which values. {_THROUGH_KEEL} {_CITE}"
    ),
    parameters={"type": "object", "properties": {}},
)

PROCESS_WAITS_SCHEMA = ToolSchema(
    name="process_waits",
    description=(
        "Read what the process instance waits for: timers, messages with their keys, signals, cases, called "
        "processes and calls to systems, and the cases and processes it started, by number. "
        f"{_THROUGH_KEEL} {_CITE}"
    ),
    parameters={"type": "object", "properties": {}},
)

PROCESS_MOVE_CHECK_SCHEMA = ToolSchema(
    name="process_move_check",
    description=(
        "Check whether the process instance can move to its process's newest version, and why not. It moves "
        f"nothing. {_THROUGH_KEEL} The answer carries a 'cite' label: cite it when you use what it says."
    ),
    parameters={"type": "object", "properties": {}},
)

PROCESS_DRAFT_FORM_SCHEMA = ToolSchema(
    name="process_draft_form",
    description=(
        "Draft values into the person's form on one of the instance's open tasks, for them to check and "
        "submit: the task's id, the values by form field, and where they come from. It drafts as the person "
        "you help, through Keel, into a task the person holds, and never chooses an outcome: that is the "
        "person's decision. Each item the process tools gave carries a 'cite' label: name in 'sources' the "
        "items your values come from."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The open task's id, as process_read gives it."},
            "values": {"type": "object", "description": "The values to draft, by form field."},
            "sources": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "object"},
                "description": "Where the values come from, each an object naming an item by its cite.",
            },
        },
        "required": ["task", "values", "sources"],
    },
)


def _keel(kwargs: dict[str, Any]) -> KeelClient | str:
    """The client this tool call asks Keel through; or the error the model reads when it reaches no Keel: its
    session is no case agent's on a process instance, or this runtime has no link to Keel."""
    if not is_process_agent_config(kwargs.get("session_config")):
        return _error(_NOT_A_PROCESS_AGENT)
    client = keel_client()
    return _error(_NO_LINK) if client is None else client


async def _read(tool: str, kwargs: dict[str, Any]) -> str:
    keel = _keel(kwargs)
    if isinstance(keel, str):
        return keel
    return answered(await keel.session_call("GET", kwargs["session_id"], _READS[tool]), tool)


async def handle_process_read(arguments: dict, **kwargs: Any) -> str:
    return await _read("process_read", kwargs)


async def handle_process_history(arguments: dict, **kwargs: Any) -> str:
    return await _read("process_history", kwargs)


async def handle_process_waits(arguments: dict, **kwargs: Any) -> str:
    return await _read("process_waits", kwargs)


async def handle_process_move_check(arguments: dict, **kwargs: Any) -> str:
    return await _read("process_move_check", kwargs)


def _draft(arguments: dict) -> dict[str, Any] | str:
    """The draft as Keel takes it, ``{"task", "values", "sources"}``; or what is wrong with it."""
    if "outcome" in arguments:
        return _OUTCOME
    task, values, sources = arguments.get("task"), arguments.get("values"), arguments.get("sources")
    if not isinstance(task, str) or not task.strip():
        return "task must be the open task's id, as process_read gives it."
    if not isinstance(values, dict):
        return "values must be an object of form field to value."
    if not isinstance(sources, list) or not sources or not all(isinstance(source, dict) for source in sources):
        return "sources must be a non-empty list of objects, each naming where the values come from."
    return {"task": task, "values": values, "sources": sources}


async def handle_process_draft_form(arguments: dict, **kwargs: Any) -> str:
    keel = _keel(kwargs)
    if isinstance(keel, str):
        return keel
    draft = _draft(arguments)
    if isinstance(draft, str):
        return _error(draft)
    answer = await _sent(
        keel.session_call("POST", kwargs["session_id"], "process/form-draft", body=draft),
        tool="process_draft_form", shorten="the values and their sources", noun="draft",
    )
    return answer if isinstance(answer, str) else answered(answer, "process_draft_form")


def register(registry: Any) -> None:
    for schema, handler in (
        (PROCESS_READ_SCHEMA, handle_process_read),
        (PROCESS_HISTORY_SCHEMA, handle_process_history),
        (PROCESS_WAITS_SCHEMA, handle_process_waits),
        (PROCESS_MOVE_CHECK_SCHEMA, handle_process_move_check),
    ):
        registry.register(
            name=schema.name, schema=schema, handler=handler, toolset="keel_process", max_result_size=_RESULT_CHARS,
        )
    registry.register(
        name="process_draft_form", schema=PROCESS_DRAFT_FORM_SCHEMA, handler=handle_process_draft_form,
        toolset="keel_process",
    )
