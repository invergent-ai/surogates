"""The ``keel_case`` tools: a person's case agent reads its case, and drafts into the person's form, through Keel.

Only a case agent's own session is offered them
(:func:`surogates.keel.steps.gate_case_tools`).  The agent never reads the
case's workspace: each tool asks Keel's door, under the session's id, and
Keel answers as the person, logging each read as a view through the case
agent.  Every item Keel gives carries a ``cite`` label the agent cites.

- ``case_read``, ``case_history`` and ``case_files`` read the case, its
  history and its files, and answer Keel's JSON as it came.
- ``case_file_read`` reads one file, as text: a document is converted, plain
  text decoded, and anything else refused, the text cut at
  :data:`CASE_TEXT_CHARS`.  The file's bytes never reach the model.
- ``case_draft_form`` drafts values into the person's form on an open step,
  each with its sources, and answers Keel's JSON as it came.

Keel's other answers come back through :func:`answered`, and a text too
long for the result is cut to fit by :func:`fit_result`: the designer's tools
share both.  No log line here carries the case's data.
"""

from __future__ import annotations

import base64
import binascii
import json
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from surogates.channels.memory_boundary import is_case_agent_config
from surogates.keel.client import KeelAnswer, KeelClient, keel_client
from surogates.tools.builtin.case_step import _error, _sent
from surogates.tools.builtin.file_ops import _DOCUMENT_EXTENSIONS, DocumentParseError, _parse_document_to_text
from surogates.tools.registry import ToolSchema

#: The longest text of a file ``case_file_read`` gives the model.
CASE_TEXT_CHARS = 200_000
#: The longest result a case tool that reads returns, as the registry counts
#: it: room for a long case's answer, and for a file's text with the escapes
#: JSON adds to it.  ``case_file_read`` shortens the text until its result
#: fits, so the registry never cuts it and ``truncated`` survives.
_RESULT_CHARS = 300_000

_NOT_A_CASE_AGENT = "This session is no person's case agent: it has no case to read through Keel."
_NO_LINK = "This runtime has no link to Keel."
_TOO_LARGE = "The file is larger than the 8 MiB Keel sends."
#: How each tool that only reads names the door's route under the session.
_READS = {"case_read": "case", "case_history": "case/history", "case_files": "case/files"}

_THROUGH_KEEL = "It reads the case as the person you help, through Keel."
_CITE = "Each item carries a 'cite' label: cite it when you use what it says."

CASE_READ_SCHEMA = ToolSchema(
    name="case_read",
    description=(
        "Read the case you work on: its number, title, type, stage and status, its data "
        "field by field, and the steps open on it, with the form fields of each and "
        f"whether it is the person's. {_THROUGH_KEEL} {_CITE}"
    ),
    parameters={"type": "object", "properties": {}},
)

CASE_HISTORY_SCHEMA = ToolSchema(
    name="case_history",
    description=(
        "Read the case's history: what happened on it, in order, each entry a sentence, "
        f"with an explanation of how to read them. {_THROUGH_KEEL} {_CITE}"
    ),
    parameters={"type": "object", "properties": {}},
)

CASE_FILES_SCHEMA = ToolSchema(
    name="case_files",
    description=(
        "List the case's files: the path, name, who added each and when. Read one with "
        f"case_file_read. {_THROUGH_KEEL} {_CITE}"
    ),
    parameters={"type": "object", "properties": {}},
)

CASE_FILE_READ_SCHEMA = ToolSchema(
    name="case_file_read",
    description=(
        "Read one of the case's files as text: a document (PDF, Word, Excel, PowerPoint, "
        f"OpenDocument, RTF) or a text file. Long text is cut, and says so. {_THROUGH_KEEL} "
        f"The file carries a 'cite' label: cite it when you use what it says."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The file's path, as case_files lists it."},
        },
        "required": ["path"],
    },
)

CASE_DRAFT_FORM_SCHEMA = ToolSchema(
    name="case_draft_form",
    description=(
        "Draft values into the person's form on one of the case's open steps, for them to "
        "check and submit: the step's token, the values by form field, and where they come "
        "from. It drafts as the person you help, through Keel, into a step the person holds. "
        "Each item the case tools gave carries a 'cite' label: name in 'sources' the items "
        "your values come from."
    ),
    parameters={
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "The open step's token, as case_read gives it."},
            "values": {"type": "object", "description": "The values to draft, by form field."},
            "sources": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "object"},
                "description": "Where the values come from, each an object naming an item by its cite.",
            },
        },
        "required": ["token", "values", "sources"],
    },
)


def answered(answer: KeelAnswer, tool: str) -> str:
    """What the model reads of Keel's *answer* to a call of *tool*.

    A 200 is Keel's JSON as it came.  Keel unreachable, busy or failing
    (:attr:`~surogates.keel.client.KeelAnswer.transient`) asks the model to
    call again.  Anything else is an error with Keel's message, its code,
    and its problems when it gave them.
    """
    if answer.status == 200:
        return json.dumps(answer.data, ensure_ascii=False)
    if answer.transient:
        return _error(f"Keel could not be reached; call {tool} again.")
    extra: dict[str, Any] = {"code": answer.code}
    problems = answer.data.get("problems") if isinstance(answer.data, dict) else None
    if problems is not None:
        extra["problems"] = problems
    return _error(answer.message or f"Keel refused the call ({answer.status}).", **extra)


async def file_text(data: bytes, path: str) -> tuple[str, bool]:
    """The text of *data*, the bytes of the case's file at *path*, and whether it was cut at :data:`CASE_TEXT_CHARS`.

    A document, by its suffix, is converted to text; anything else is
    decoded as UTF-8 when it holds no NUL.  Raises :class:`ValueError`, with
    what the model reads, for neither, and for a document that cannot be
    converted.
    """
    name = PurePosixPath(path).name
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _DOCUMENT_EXTENSIONS:
        with tempfile.TemporaryDirectory() as scratch:
            document = Path(scratch) / f"document{suffix}"
            document.write_bytes(data)
            try:
                text = await _parse_document_to_text(document)
            except DocumentParseError:
                raise ValueError(f"{name} could not be read as a document.") from None
    else:
        try:
            text = data.decode("utf-8") if b"\x00" not in data else None
        except UnicodeDecodeError:
            text = None
        if text is None:
            raise ValueError(f"{name} is neither a document nor text: case_file_read cannot read it.")
    return text[:CASE_TEXT_CHARS], len(text) > CASE_TEXT_CHARS


def _keel(kwargs: dict[str, Any]) -> KeelClient | str:
    """The client this tool call asks Keel through; or the error the model reads when it reaches no Keel: its
    session is no case agent's, or this runtime has no link to Keel."""
    if not is_case_agent_config(kwargs.get("session_config")):
        return _error(_NOT_A_CASE_AGENT)
    client = keel_client()
    return _error(_NO_LINK) if client is None else client


async def _read(tool: str, kwargs: dict[str, Any]) -> str:
    keel = _keel(kwargs)
    if isinstance(keel, str):
        return keel
    return answered(await keel.session_call("GET", kwargs["session_id"], _READS[tool]), tool)


async def handle_case_read(arguments: dict, **kwargs: Any) -> str:
    return await _read("case_read", kwargs)


async def handle_case_history(arguments: dict, **kwargs: Any) -> str:
    return await _read("case_history", kwargs)


async def handle_case_files(arguments: dict, **kwargs: Any) -> str:
    return await _read("case_files", kwargs)


async def handle_case_file_read(arguments: dict, **kwargs: Any) -> str:
    keel = _keel(kwargs)
    if isinstance(keel, str):
        return keel
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        return _error("path must name one of the case's files, as case_files lists it.")
    answer = await keel.session_call("GET", kwargs["session_id"], "case/file", params={"path": path})
    if answer.status == 413 and answer.code == "too-large":
        return _error(_TOO_LARGE)
    if answer.status != 200:
        return answered(answer, "case_file_read")
    sent = answer.data if isinstance(answer.data, dict) else {}
    named = sent.get("path") if isinstance(sent.get("path"), str) else path
    try:
        data = base64.b64decode(sent.get("content_base64"), validate=True)
    except (binascii.Error, TypeError, ValueError):
        return _error("Keel sent the file in a form this runtime cannot read.")
    try:
        text, truncated = await file_text(data, named)
    except ValueError as exc:
        return _error(str(exc))
    return _file_result(named, sent.get("cite"), text, truncated)


def _file_result(path: str, cite: Any, text: str, truncated: bool) -> str:
    """``case_file_read``'s result, ``{"path", "cite", "text", "truncated"}``, as JSON of at most :data:`_RESULT_CHARS`."""
    return fit_result({"path": path, "cite": cite, "text": text, "truncated": truncated}, "text")


def fit_result(fields: dict[str, Any], key: str) -> str:
    """*fields*, which hold a text under *key* and ``truncated``, as JSON of at most :data:`_RESULT_CHARS`.

    Escapes can make the JSON of a text far longer than the text: the text
    is then shortened until the result fits, and ``truncated`` is true.
    Each character cut takes at least one off the JSON.
    """
    fields = dict(fields)
    while True:
        result = json.dumps(fields, ensure_ascii=False)
        over = len(result) - _RESULT_CHARS
        text = fields[key]
        if over <= 0 or not text:
            return result
        fields[key], fields["truncated"] = text[: max(len(text) - over, 0)], True


def _draft(arguments: dict) -> dict[str, Any] | str:
    """The draft as Keel takes it, ``{"token", "values", "sources"}``; or what is wrong with it."""
    token, values, sources = arguments.get("token"), arguments.get("values"), arguments.get("sources")
    if not isinstance(token, str) or not token.strip():
        return "token must be the open step's token, as case_read gives it."
    if not isinstance(values, dict):
        return "values must be an object of form field to value."
    if not isinstance(sources, list) or not sources or not all(isinstance(source, dict) for source in sources):
        return "sources must be a non-empty list of objects, each naming where the values come from."
    return {"token": token, "values": values, "sources": sources}


async def handle_case_draft_form(arguments: dict, **kwargs: Any) -> str:
    keel = _keel(kwargs)
    if isinstance(keel, str):
        return keel
    draft = _draft(arguments)
    if isinstance(draft, str):
        return _error(draft)
    answer = await _sent(
        keel.session_call("POST", kwargs["session_id"], "case/form-draft", body=draft),
        tool="case_draft_form", shorten="the values and their sources", noun="draft",
    )
    return answer if isinstance(answer, str) else answered(answer, "case_draft_form")


def register(registry: Any) -> None:
    for schema, handler in (
        (CASE_READ_SCHEMA, handle_case_read),
        (CASE_HISTORY_SCHEMA, handle_case_history),
        (CASE_FILES_SCHEMA, handle_case_files),
        (CASE_FILE_READ_SCHEMA, handle_case_file_read),
    ):
        registry.register(
            name=schema.name, schema=schema, handler=handler, toolset="keel_case", max_result_size=_RESULT_CHARS,
        )
    registry.register(
        name="case_draft_form", schema=CASE_DRAFT_FORM_SCHEMA, handler=handle_case_draft_form, toolset="keel_case",
    )
