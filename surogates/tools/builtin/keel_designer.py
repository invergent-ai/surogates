"""The ``keel_designer`` tools: the designer agent works on the draft a builder opened it on, through Keel.

Only an agent whose policy's allow-list names them is offered them, and no
case's session (:data:`surogates.runtime.governance.KEEL_DESIGNER_TOOLS`).
Each tool asks Keel's door, under the session's id: Keel finds the draft the
builder opened the designer on, and acts on it as the builder.  None
publishes: publishing stays the builder's.

- ``draft_files`` lists the draft's files, and ``draft_read`` reads one.
- ``draft_main`` lists the project's published definitions, or reads one.
- ``draft_save_file`` and ``draft_save_scenario`` save a definition's file
  or a test scenario, whole.
- ``draft_dry_run`` dry-runs the draft.
- ``draft_process_read`` reads a process file of the draft, or of the
  published definitions, as BPMN XML without its diagram;
  ``draft_process_check`` checks it as a publish would;
  ``draft_process_reference`` reads Keel's reference for process files.

Each answers Keel's JSON as it came, through
:func:`~surogates.tools.builtin.keel_case.answered`.  A file too long to give
whole comes back as its text, cut, with ``truncated`` true; a process file
that comes back cut is changed only by edits, never saved whole.  No log
line here carries a file's content or path.
"""

from __future__ import annotations

import json
import re
from typing import Any

from surogates.channels.memory_boundary import is_case_config
from surogates.keel.client import MAX_DRAFT_BYTES, BodyNotJSON, BodyTooLarge, KeelAnswer, KeelClient, keel_client
from surogates.tools.builtin.case_step import _error
from surogates.tools.builtin.keel_case import _NO_LINK, _RESULT_CHARS, answered, fit_result
from surogates.tools.registry import ToolSchema

_IN_A_CASE = "This session works on a case, not on a draft: the designer's tools act only in a designer's chat."
_NO_DELEGATION = "No draft is open to this chat: ask the builder to open the designer on a draft."
_TOO_LARGE = "The file is over the 1 MiB Keel takes: split it or shorten it."

_ON_THE_DRAFT = (
    "It acts, through Keel, on the one draft the builder opened the designer on, and never publishes: "
    "publishing stays the builder's."
)
_CUT = "A file too long to give whole comes back as its JSON text, cut, with truncated true."

#: A process's file in the draft: ``processes/<key>.bpmn``, as ``draft_files`` lists it.
PROCESS_PATH = re.compile(r"^processes/[^/]+\.bpmn$")
_NOT_A_PROCESS_PATH = "path must name a process's file, processes/<key>.bpmn, as draft_files lists it."
_NOT_PUBLISHED = "published must be true or false, or be left out."
#: What a Keel that lacks the process routes answers: its own 404 for a path it does not serve.
_OLD_KEEL = (
    "This Keel cannot work on process files yet: tell the builder you can work on its case types, and on its "
    "processes once Keel is upgraded."
)
PROCESS_TOOLS = frozenset({
    "draft_process_read", "draft_process_save", "draft_process_edit", "draft_process_check",
    "draft_process_reference",
})

DRAFT_FILES_SCHEMA = ToolSchema(
    name="draft_files",
    description=f"List the draft's files: the path, kind and key of each. Read one with draft_read. {_ON_THE_DRAFT}",
    parameters={"type": "object", "properties": {}},
)

DRAFT_READ_SCHEMA = ToolSchema(
    name="draft_read",
    description=f"Read one of the draft's files: its path and its JSON content. {_CUT} {_ON_THE_DRAFT}",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The file's path, as draft_files lists it."},
        },
        "required": ["path"],
    },
)

DRAFT_MAIN_SCHEMA = ToolSchema(
    name="draft_main",
    description=(
        "Read the project's published definitions, which the draft changes: without a path, the list of "
        f"their files; with one, that file's path and JSON content. {_CUT} {_ON_THE_DRAFT}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "A published file's path, as draft_main lists them. Leave it out for the list.",
            },
        },
    },
)

DRAFT_SAVE_FILE_SCHEMA = ToolSchema(
    name="draft_save_file",
    description=(
        "Save one of the draft's definition files, whole: its path and its JSON content. Keel checks the "
        "file, and answers the draft's new head, or what is wrong with the file. "
        f"{_ON_THE_DRAFT}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The file's path in the draft, as draft_files lists it."},
            "content": {"type": "object", "description": "The file's whole JSON content."},
        },
        "required": ["path", "content"],
    },
)

DRAFT_SAVE_SCENARIO_SCHEMA = ToolSchema(
    name="draft_save_scenario",
    description=(
        "Save one of the draft's test scenarios, whole: its path and its JSON content. Keel answers the "
        f"draft's new head. {_ON_THE_DRAFT}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The scenario's path in the draft."},
            "content": {"type": "object", "description": "The scenario's whole JSON content."},
        },
        "required": ["path", "content"],
    },
)

DRAFT_DRY_RUN_SCHEMA = ToolSchema(
    name="draft_dry_run",
    description=(
        "Dry-run the draft: what publishing it would find, each problem with where it is, the scenarios "
        "that played, and the head it checked. Its ops_checked is false: ops was not asked about the "
        f"project's agents and knowledge bases. {_ON_THE_DRAFT}"
    ),
    parameters={"type": "object", "properties": {}},
)

DRAFT_PROCESS_READ_SCHEMA = ToolSchema(
    name="draft_process_read",
    description=(
        "Read one of the draft's process files, or with published the project's published one, as BPMN XML "
        "without its diagram: its path and its text. Keel draws the diagram itself, so the text holds none. "
        "A file too long to give whole comes back cut, with truncated true: change such a file only with "
        f"draft_process_edit. {_ON_THE_DRAFT}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The process's file, processes/<key>.bpmn, as draft_files or draft_main lists it.",
            },
            "published": {
                "type": "boolean",
                "description": (
                    "True to read the project's published file instead of the draft's. Leave it out for the "
                    "draft's."
                ),
            },
        },
        "required": ["path"],
    },
)

DRAFT_PROCESS_CHECK_SCHEMA = ToolSchema(
    name="draft_process_check",
    description=(
        "Check one of the draft's process files as a publish would: each problem Keel finds in it, or in the "
        "draft about it, with its code, where it is, its message and whether it is an error or a warning. Its "
        "ops_checked is false: ops was not asked about the project's agents and knowledge bases. "
        f"{_ON_THE_DRAFT}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The process's file in the draft, processes/<key>.bpmn.",
            },
        },
        "required": ["path"],
    },
)

DRAFT_PROCESS_REFERENCE_SCHEMA = ToolSchema(
    name="draft_process_reference",
    description=(
        "Read Keel's reference for process files: the BPMN elements Keel runs, every keel: setting with the "
        "values it takes, every problem Keel reports with what fixes it, and worked examples. Read it before "
        "you write or change a process."
    ),
    parameters={"type": "object", "properties": {}},
)


def _keel(kwargs: dict[str, Any]) -> KeelClient | str:
    """The client this tool call asks Keel through; or the error the model reads when it reaches no Keel: its
    session works on a case, or this runtime has no link to Keel."""
    if is_case_config(kwargs.get("session_config")):
        return _error(_IN_A_CASE)
    client = keel_client()
    return _error(_NO_LINK) if client is None else client


def _told(answer: KeelAnswer, tool: str) -> str:
    """What the model reads of Keel's *answer* to a call of *tool*: :func:`answered`'s text, but its own for a
    session that holds no draft."""
    if answer.status == 404 and answer.code == "no-delegation":
        return _error(_NO_DELEGATION)
    if answer.status == 404 and answer.code == "not-found" and tool in PROCESS_TOOLS:
        return _error(_OLD_KEEL)
    return answered(answer, tool)


def _path(arguments: dict) -> str | None:
    """The path the call names; ``None`` when it names none."""
    path = arguments.get("path")
    return path if isinstance(path, str) and path.strip() else None


async def _get(
    tool: str, route: str, kwargs: dict[str, Any], path: str | None = None, text_key: str = "content",
) -> str:
    """What the model reads of Keel's answer to *tool*'s read of *route*, with *path* as the query when
    given: a file too long to give whole is cut to fit (:func:`fit_result`).  A definition's file is JSON,
    given as text under ``content``; a process's file is text already, under *text_key* ``xml``."""
    keel = _keel(kwargs)
    if isinstance(keel, str):
        return keel
    answer = await keel.session_call(
        "GET", kwargs["session_id"], route, params={"path": path} if path is not None else None,
    )
    told = _told(answer, tool)
    if answer.status != 200 or len(told) <= _RESULT_CHARS or path is None:
        return told
    sent = answer.data if isinstance(answer.data, dict) else {}
    named = sent.get("path") if isinstance(sent.get("path"), str) else path
    if text_key == "xml":
        text = sent.get("xml") if isinstance(sent.get("xml"), str) else ""
    else:
        text = json.dumps(sent.get("content"), ensure_ascii=False)
    return fit_result({"path": named, text_key: text, "truncated": False}, text_key)


async def _save(tool: str, route: str, arguments: dict, kwargs: dict[str, Any], noun: str) -> str:
    """Save the file *arguments* give, a *noun*, at *route*, and tell the model what Keel answered."""
    keel = _keel(kwargs)
    if isinstance(keel, str):
        return keel
    path, content = _path(arguments), arguments.get("content")
    if path is None:
        return _error(f"path must name the {noun} in the draft.")
    if not isinstance(content, dict):
        return _error(f"content must be the {noun}'s whole JSON object.")
    try:
        answer = await keel.session_call(
            "PUT", kwargs["session_id"], route, params={"path": path}, body=content, limit=MAX_DRAFT_BYTES,
        )
    except BodyTooLarge:
        return _error(_TOO_LARGE)
    except BodyNotJSON:
        return _error(
            f"The {noun} holds a number JSON cannot carry (NaN or Infinity). Give a real number, or leave "
            f"the value out, and call {tool} again.",
        )
    return _told(answer, tool)


async def handle_draft_files(arguments: dict, **kwargs: Any) -> str:
    return await _get("draft_files", "draft/files", kwargs)


async def handle_draft_read(arguments: dict, **kwargs: Any) -> str:
    path = _path(arguments)
    if path is None:
        return _error("path must name one of the draft's files, as draft_files lists it.")
    return await _get("draft_read", "draft/file", kwargs, path)


async def handle_draft_main(arguments: dict, **kwargs: Any) -> str:
    if arguments.get("path") is None:
        return await _get("draft_main", "draft/main", kwargs)
    path = _path(arguments)
    if path is None:
        return _error("path must name one of the published files, as draft_main lists them, or be left out.")
    return await _get("draft_main", "draft/main", kwargs, path)


async def handle_draft_save_file(arguments: dict, **kwargs: Any) -> str:
    return await _save("draft_save_file", "draft/file", arguments, kwargs, "file")


async def handle_draft_save_scenario(arguments: dict, **kwargs: Any) -> str:
    return await _save("draft_save_scenario", "draft/scenario", arguments, kwargs, "scenario")


async def handle_draft_dry_run(arguments: dict, **kwargs: Any) -> str:
    keel = _keel(kwargs)
    if isinstance(keel, str):
        return keel
    return _told(await keel.session_call("POST", kwargs["session_id"], "draft/dry-run"), "draft_dry_run")


def _process_path(arguments: dict) -> str | None:
    """The process file the call names; ``None`` when it names none."""
    path = arguments.get("path")
    return path if isinstance(path, str) and PROCESS_PATH.match(path) else None


async def handle_draft_process_read(arguments: dict, **kwargs: Any) -> str:
    path = _process_path(arguments)
    if path is None:
        return _error(_NOT_A_PROCESS_PATH)
    published = arguments.get("published", False) if "published" in arguments else False
    if not isinstance(published, bool):
        return _error(_NOT_PUBLISHED)
    route = "draft/main/process" if published else "draft/process"
    return await _get("draft_process_read", route, kwargs, path, text_key="xml")


async def handle_draft_process_check(arguments: dict, **kwargs: Any) -> str:
    path = _process_path(arguments)
    if path is None:
        return _error(_NOT_A_PROCESS_PATH)
    keel = _keel(kwargs)
    if isinstance(keel, str):
        return keel
    answer = await keel.session_call("POST", kwargs["session_id"], "draft/process/check", params={"path": path})
    return _told(answer, "draft_process_check")


async def handle_draft_process_reference(arguments: dict, **kwargs: Any) -> str:
    return await _get("draft_process_reference", "draft/process/reference", kwargs)


def register(registry: Any) -> None:
    # Each with room for a long file or a long list of problems, so its JSON comes whole.
    for schema, handler in (
        (DRAFT_FILES_SCHEMA, handle_draft_files),
        (DRAFT_READ_SCHEMA, handle_draft_read),
        (DRAFT_MAIN_SCHEMA, handle_draft_main),
        (DRAFT_SAVE_FILE_SCHEMA, handle_draft_save_file),
        (DRAFT_SAVE_SCENARIO_SCHEMA, handle_draft_save_scenario),
        (DRAFT_DRY_RUN_SCHEMA, handle_draft_dry_run),
        (DRAFT_PROCESS_READ_SCHEMA, handle_draft_process_read),
        (DRAFT_PROCESS_CHECK_SCHEMA, handle_draft_process_check),
        (DRAFT_PROCESS_REFERENCE_SCHEMA, handle_draft_process_reference),
    ):
        registry.register(
            name=schema.name, schema=schema, handler=handler, toolset="keel_designer", max_result_size=_RESULT_CHARS,
        )
