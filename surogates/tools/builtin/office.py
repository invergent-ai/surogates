"""office_read and office_edit: the agent reads and edits a document through its own live editor session.

The document is the one the user may have open in the chat's file pane: each office_edit call appears there whole,
under the agent's name. runtime-office holds the agent's editor page; this module opens it and passes the call on.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from PIL import Image

from surogates.channels.memory_boundary import is_case_session
from surogates.config import load_settings
from surogates.devices.workspace import DeviceOperationError, said
from surogates.office.agent import AGENT_KINDS, AgentDocument, AgentDocumentError, open_for_agent, relative_path, within_folder
from surogates.office.guide import GUIDES_KEY, guide_for
from surogates.office.kinds import office_kind
from surogates.office.mentions import ACKNOWLEDGEMENT
from surogates.office.ops_input import OpsInputError, read_ops
from surogates.office.runtime_office import OfficeBusy, PageGone, RuntimeOffice, RuntimeOfficeError
from surogates.session.files import ComputerAway, session_files
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
# Within runtime-office's 20 MiB request once base64 makes them a third larger, with room for the rest of the call.
MAX_PICTURES_BYTES = 12 * 1024 * 1024
# A slide's <img src="…">: a workspace path the measuring page could not fetch (it fetches nothing), made a data URL.
_IMG_SRC = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)
_NAME_TTL_S = 300.0
# A result with its guide stays this short: the tool registry cuts a result at 50 000 characters.
MAX_GUIDED_RESULT = 45_000
# The agent's display name by agent id, read from its runtime config at most every five minutes.
_names: dict[str, tuple[float, str]] = {}

READ_DESCRIPTION = (
    "Read a Word document (.docx), spreadsheet (.xlsx), slide deck (.pptx) or PDF (.pdf) in the workspace through "
    "your own live editor session: the one the user may have open in the file pane, with everyone's latest edits. Read "
    "before every office_edit: what it shows is what office_edit targets. Read again to check your edits before you "
    "answer: once it shows a change, the document has it (a file read any other way can lag the editor).\n"
    'view "outline" (default): a Word document\'s blocks (id, hash, kind, first words; comment threads, tracked '
    "changes; a long one in pages: start=<next>); a spreadsheet's sheets (used range, top rows, merged cells, charts) "
    "and comment threads; a deck's slides (id, number, title, hash) and comment threads; a PDF's pages (first line, "
    "form fields, a checkbox as true/false, annotations, each a comment thread with its replies; start=<next>).\n"
    'One in full: view "block" (Word; target = a block id), "range" (spreadsheet; target = "Sheet!A1:D20": the cells '
    'as shown, their formulas and number formats), "slide" (deck; target = a slide id; every shape in canvas px), '
    '"page" (PDF; target = a page number; its text).\n'
    'view "search" (query) finds words: their blocks, cells, slides or pages.\n'
    "The result says who else has the document open. Your first office call on each document type also brings that "
    "type's rules, reference and templates, as guide."
)

EDIT_DESCRIPTION = (
    "Word documents (.docx), spreadsheets (.xlsx) and slide decks (.pptx), and a PDF's form fields, pages, redactions "
    "and notes, are made and changed only with office_edit, new ones too: never with a script or a python library "
    "(python-docx, openpyxl, python-pptx, pypdf), write_file, patch or the terminal: the user can neither watch nor "
    "edit a document made that way. What office_edit cannot do to a PDF (a new PDF with content; merging, splitting or "
    "encrypting PDFs) is the pdf skill's work. A missing .docx, .xlsx, .pptx or .pdf is created blank and opens in the "
    "user's pane, where the user watches each call's changes appear under your name, and may be editing too.\n"
    "One complete call for each type, to start from; a new document starts from the office skill's "
    "templates/<type>.md. A new Word document's outline, each section a pending block that a later call fills "
    '(replace, target {"pending": "goals"}):\n'
    "```json\n"
    '{"path": "plans/launch.docx", "ops": [{"op": "insert", "target": {"end": true}, "content": {"markdown": '
    '"# Launch plan\\n\\n## Goals\\n<?pending goals: Three goals, each with its date if given — 60 words?>'
    '\\n\\n## Budget\\n<?pending budget: Costs by quarter, in a table — 80 words?>"}}]}\n'
    "```\n"
    "A spreadsheet's cells and a bold header:\n"
    "```json\n"
    '{"path": "data/sales.xlsx", "ops": [{"op": "set", "range": "Sheet1!A1", "values": [["Region", "Q1", "Q2", '
    '"Total"], ["North", 120, 140, "=B2+C2"]]}, {"op": "format", "range": "Sheet1!A1:D1", "bold": true}]}\n'
    "```\n"
    "A new slide:\n"
    "```json\n"
    '{"path": "decks/q3.pptx", "ops": [{"op": "slides", "slides": [{"html": "<section style=\'background:#FBFBF8;'
    "color:#14285A;font-family:Arial;padding:128px;gap:40px'><h1 style='font-size:128px'>Q3 results</h1><p>"
    'Revenue grew 12%, led by the north</p></section>"}]}]}\n'
    "```\n"
    "A PDF form's fields, named as the outline shows them:\n"
    "```json\n"
    '{"path": "forms/application.pdf", "ops": [{"op": "fill", "fields": {"full_name": "Ana Silva", "agree": true}}]}\n'
    "```\n"
    "About 450 words of body text fill a page: for a length the user asks for, count the words (a Word edit's result "
    "gives the document's length). Check with office_read before you answer: read the outline again; every fact the "
    "user gave is in the document; no name, date, figure or contact they did not give is made up, and a part they "
    "asked for but did not detail is drafted as a proposal, not asked about; the length is what was asked; then say "
    "in one line what was done and what you drafted for them to check.\n"
    "One piece of work per call (a section, a block of cells, a slide), with a short line in the chat between calls. "
    "A call's ops apply together or not at all. If what you target changed since you read it, nothing applies and the "
    "result shows it as it is now: the user's edit wins; read again and work from it.\n"
    'Word: insert {target, side: before|after, content} (list items next to a list item continue its list; "list": '
    '"new" starts a new one), replace {target, content} (on quoted words, '
    '{"text": "…"} re-words them keeping their formatting), delete {target}, move {target, to, side}, comment '
    '{anchor: {find}, body}, reply {thread, body}, resolve {thread}; a replaced or moved block keeps its comment '
    'threads. Targets: {"block": "<id>", "hash": "<hash>"}, {"find": "exact words", "nth": 1}, {"pending": "<key>"}, '
    '{"end": true}. Content: {"markdown": "…", "blocks": {…}}; a line <?pending k: what it will hold?> (k: one word) '
    'is a pending block; a line <?block k?> places blocks[k]: {"type": "chart", "chart": "bar|column|line|pie|…", '
    '"title": "…", "rows": [["", "A"], ["x", 1]]} or {"type": "image", "path": "<PNG or JPEG>", "width": <px>}.\n'
    'Spreadsheet: set {range: "Sheet!A1", values: [[…]], expect?: [[the range as you read it]]} (each value is typed '
    'in as into a cell: "=…" is a live formula, a leading \' keeps text); format {range, number_format, bold, italic, '
    "fill, color, align, valign, wrap, width}; chart {range, type: column|bar|line|pie|area|doughnut|scatter, title, "
    'anchor: "F2"}; sheet {action: add|rename|delete, name, to, after}.\n'
    "Deck: text {slide, hash, shape, text} changes one shape's text and keeps everything else (its place, style and "
    "placeholder; the slide's notes): use it for a small change, a title, a word, a number (shape ids from view "
    '"slide"). notes {slide, hash, text} sets a slide\'s speaker notes and changes nothing else. slides {slides: '
    "[{html, after?: <slide id>}, or {id, hash, html} to replace one]} adds or rebuilds whole "
    "slides, each one <section> in the Claude Slides format on a 1920 px wide canvas, built as native shapes; order "
    "{ids: every slide, in order}; remove {slides: [{id, hash}]}. The slide format is strict: your first office call on "
    "a deck brings its reference.\n"
    "PDF: pages {action: add|remove|rotate, page, degrees}; fill {fields: {name: text or true/false}, expect?: {name: "
    "its value as the outline showed it}} (a field changed since your read refuses the call); add_field {type: "
    "text|checkbox, page, rect: [x1, y1, x2, y2] in points from the page's top-left, name}; redact {text}, alone in "
    "its call (gone for good once saved); annotate {kind: free_text|stamp, anchor: {find, nth?} | page + rect, text | "
    "stamp}. A PDF's existing text cannot be changed.\n"
    "Comment threads, in every type: reply {thread, body} and resolve {thread}, where thread is a thread's id as "
    "office_read's outline shows it. In a spreadsheet, deck or PDF they go in a call of their own. A reply in a thread "
    "states only the result, in one or two sentences (at most 400 characters): your reasoning, the steps and the "
    "conversation stay in the chat. Resolve last."
)

READ_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The document's path in the workspace."},
        "view": {"type": "string", "enum": ["outline", "block", "range", "slide", "page", "search"]},
        "target": {"type": "string", "description": 'The block id, the range ("Sheet!A1:D20"), the slide id or the page number to view.'},
        "query": {"type": "string", "description": 'view "search": the words to find.'},
        "start": {"type": "integer", "description": 'view "outline": where the page starts (the previous page\'s next).'},
    },
    "required": ["path"],
}

EDIT_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The document's path in the workspace."},
        "ops": {
            "type": "array",
            "description": "The ops, applied in order, all or none.",
            "items": {"type": "object", "properties": {"op": {"type": "string"}}, "required": ["op"]},
        },
    },
    "required": ["path", "ops"],
}

Call = Callable[[RuntimeOffice, AgentDocument, Any], Awaitable[dict[str, Any]]]
# What a call reads of its arguments for the document's kind before the document opens: what its answer says of them.
BeforeOpen = Callable[[str], dict[str, Any]]


def register(registry: ToolRegistry) -> None:
    """Registered only where the agent edits live (a document server and runtime-office): elsewhere it does not exist."""
    if not load_settings().office.agent_editing:
        return
    registry.register(
        name="office_read",
        schema=ToolSchema(name="office_read", description=READ_DESCRIPTION, parameters=READ_PARAMETERS),
        handler=_office_read,
        toolset="office",
    )
    registry.register(
        name="office_edit",
        schema=ToolSchema(name="office_edit", description=EDIT_DESCRIPTION, parameters=EDIT_PARAMETERS),
        handler=_office_edit,
        toolset="office",
    )


async def _office_read(arguments: dict[str, Any], **kwargs: Any) -> str:
    view = arguments.get("view") or "outline"
    if view == "outline":
        request: dict[str, Any] = {"view": "outline", "start": int(arguments.get("start") or 0)}
    elif view in ("block", "range", "slide", "page") and str(arguments.get("target") or "").strip():
        request = {"view": view, "target": str(arguments["target"]).strip()}
    elif view == "search" and arguments.get("query"):
        request = {"view": "search", "query": str(arguments["query"])}
    else:
        return _error('view is "outline"; "block", "range", "slide" or "page" with a target; or "search" with a query.')

    async def call(runtime: RuntimeOffice, document: AgentDocument, session: Any) -> dict[str, Any]:
        return await runtime.read(document.page_id, request)

    return await _run(arguments, kwargs, create=False, call=call)


async def _office_edit(arguments: dict[str, Any], **kwargs: Any) -> str:
    ops: list[dict[str, Any]] = []

    def read(kind: str) -> dict[str, Any]:
        nonlocal ops
        ops, read_as = read_ops(arguments.get("ops"), kind)
        return _read_as(read_as)

    async def call(runtime: RuntimeOffice, document: AgentDocument, session: Any) -> dict[str, Any]:
        return await runtime.edit(document.page_id, await _with_pictures(ops, session, kwargs))

    return await _run(arguments, kwargs, create=True, call=call, before_open=read)


async def acknowledge_mention(mention: dict[str, str], **kwargs: Any) -> str:
    """Post ACKNOWLEDGEMENT in the thread a mention came from, through the agent's editor session as office_edit edits;
    the answer as office_edit gives it. A PDF's mention carries no lasting thread id: its thread is found by the
    comment's words and its author."""

    async def call(runtime: RuntimeOffice, document: AgentDocument, session: Any) -> dict[str, Any]:
        thread: str | None = mention["thread"]
        if mention["kind"] == "pdf":
            found = await runtime.read(document.page_id, {"view": "thread", "query": mention["comment"], "author": mention["author_id"]})
            thread = (found.get("thread") or {}).get("id")
            if not thread:
                raise AgentDocumentError(f"The comment that mentioned you is not in {document.path} any more.")
        return await runtime.edit(document.page_id, [{"op": "reply", "thread": thread, "body": ACKNOWLEDGEMENT}])

    return await _run({"path": mention["path"]}, kwargs, create=False, call=call)


async def _run(arguments: dict[str, Any], kwargs: dict[str, Any], *, create: bool, call: Call,
               before_open: BeforeOpen | None = None) -> str:
    office = load_settings().office
    raw_path = str(arguments.get("path") or "").strip()
    if not raw_path:
        return _error('"path" names the document in the workspace.')
    session = await kwargs["session_store"].get_session(UUID(str(kwargs["session_id"])))
    path = relative_path(raw_path, session)
    if not within_folder(path):
        return _error("Name the document by its path in the chat's folder, like docs/report.docx.")
    kind = office_kind(path)
    # The document types the agent works on; any other kind is refused by opening.
    file_type = kind.file_type if kind is not None and kind.file_type in AGENT_KINDS else None
    reading: dict[str, Any] = {}
    # A call that cannot be read opens nothing and makes nothing.
    if before_open is not None and file_type is not None:
        try:
            reading = before_open(file_type)
        except OpsInputError as exc:
            # Refused for what it says: the type's rules, reference and templates are what the model needs now.
            return await _with_guide({"error": str(exc), **_read_as(exc.read_as)}, file_type, session, kwargs)
    agent_id = str(kwargs.get("agent_id") or session.agent_id)
    runtime = RuntimeOffice(office)
    try:
        name = await _agent_name(kwargs.get("platform_client"), agent_id)
        for attempt in range(2):
            document = await open_for_agent(
                session=session, agent_id=agent_id, agent_name=name, path=path, create=create, office=office,
                runtime=runtime, storage=kwargs["storage"], session_factory=kwargs["session_factory"], redis=kwargs["redis"],
            )
            try:
                result = await call(runtime, document, session)
                break
            except PageGone:
                # runtime-office restarted, or a conflict dropped the editor: one fresh page.
                if attempt:
                    raise
    except AgentDocumentError as exc:
        return _error(str(exc), **reading)
    except OfficeBusy:
        if is_case_session(session):
            # A case's document path is the case's data.
            logger.warning("runtime-office is full (session %s)", session.id)
        else:
            logger.warning("runtime-office is full: %s in session %s", path, session.id)
        return _error("The office service has as many documents open as it can hold; try again in a minute.", **reading)
    except RuntimeOfficeError as exc:
        if exc.status == 400:
            # Refused for what it says (a slide's format or fit, a Word block's content, an op): as above.
            return await _with_guide({"error": str(exc), **reading}, file_type, session, kwargs)
        if is_case_session(session):
            # The error's words, and its traceback, can name the case's document.
            logger.warning("runtime-office failed (session %s): %s", session.id, type(exc).__name__)
        else:
            logger.warning("runtime-office failed on %s", path, exc_info=True)
        return _error(f"The editor could not do that: {exc}", **reading)
    # Named as its saves and notices name it, the computer's spelling of it: the next call finds it at once.
    answer: dict[str, Any] = {"document": document.document.path, "open_by": _open_by(document), **reading}
    if document.created:
        answer["created"] = True
    if result.get("flushed") is False:
        answer["note"] = "The edit applied in your editor but had not reached the others' editors after 10 seconds."
    answer = {**answer, **result}
    if "error" in answer:
        return json.dumps(answer, ensure_ascii=False)
    return await _with_guide(answer, file_type, session, kwargs)


async def _with_guide(answer: dict[str, Any], kind: str | None, session: Any, kwargs: dict[str, Any]) -> str:
    """*answer* as JSON, carrying the office skill's rules and *kind*'s reference and templates on the model's first
    call on that type in the session, done or refused for what it said, which then records the type as given. A guide
    that would make the result MAX_GUIDED_RESULT long or more goes without its templates (the skill keeps them), and is
    left for the next call when even that is too long. Only a model's call takes it: the worker's own (a mention's
    acknowledgement) never does."""
    text = json.dumps(answer, ensure_ascii=False)
    given = (session.config or {}).get(GUIDES_KEY) or []
    if kind is None or not kwargs.get("tool_call_id") or kind in given or len(text) >= MAX_GUIDED_RESULT:
        return text
    guide = await guide_for(kind, kwargs.get("api_client"))
    if guide is None:
        return text
    guided = json.dumps({**answer, "guide": guide}, ensure_ascii=False)
    if len(guided) >= MAX_GUIDED_RESULT and "templates" in guide:
        guide = {"rules": guide["rules"], "reference": guide["reference"]}
        guided = json.dumps({**answer, "guide": guide}, ensure_ascii=False)
    if len(guided) >= MAX_GUIDED_RESULT:
        return text
    try:
        await kwargs["session_store"].append_session_config_list(session.id, GUIDES_KEY, kind)
    except Exception:
        # The answer stands, guide and all: the next call on this type carries the guide again.
        logger.warning("could not record the %s guide as given in session %s", kind, session.id, exc_info=True)
    return guided


def _open_by(document: AgentDocument) -> list[str]:
    """Who else has the document open, as of the document server's last report: people by name, and other chats' agents."""
    names: list[str] = []
    for user in document.document.editors:
        if user == document.user_id:
            continue
        names.append(document.document.user_names.get(user) or ("another chat's agent" if user.startswith("agent:") else "someone"))
    return names


async def _agent_name(platform_client: Any, agent_id: str) -> str:
    cached = _names.get(agent_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    name = "Agent"
    if platform_client is not None:
        try:
            payload = await platform_client.get_runtime_config(agent_id)
            name = str(payload.get("display_name") or name)
        except Exception:
            logger.warning("could not read agent %s's name for the editor", agent_id, exc_info=True)
    _names[agent_id] = (time.monotonic() + _NAME_TTL_S, name)
    return name


async def _with_pictures(ops: list[Any], session: Any, kwargs: dict[str, Any]) -> list[Any]:
    """*ops* with every picture they name in the workspace read into the bytes the editor inserts."""
    budget = [MAX_PICTURES_BYTES]
    out: list[Any] = []
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "slides" and isinstance(op.get("slides"), list):
            out.append({**op, "slides": [
                {**slide, "html": await _slide_pictures(slide["html"], session, kwargs, budget)}
                if isinstance(slide, dict) and isinstance(slide.get("html"), str) else slide
                for slide in op["slides"]
            ]})
            continue
        content = op.get("content") if isinstance(op, dict) else None
        blocks = content.get("blocks") if isinstance(content, dict) else None
        if not isinstance(blocks, dict) or not any(isinstance(b, dict) and b.get("type") == "image" for b in blocks.values()):
            out.append(op)
            continue
        read = {
            name: await _picture(block, session, kwargs) if isinstance(block, dict) and block.get("type") == "image" else block
            for name, block in blocks.items()
        }
        out.append({**op, "content": {**content, "blocks": read}})
    return out


async def _slide_pictures(html: str, session: Any, kwargs: dict[str, Any], budget: list[int]) -> str:
    parts: list[str] = []
    last = 0
    for match in _IMG_SRC.finditer(html):
        src = match.group(3).strip()
        if src.startswith("data:"):
            continue
        data, mime = await _picture_bytes(src, session, kwargs, svg=True)
        budget[0] -= len(data)
        if budget[0] < 0:
            raise AgentDocumentError("One call's pictures are at most 12 MB together; build the slides over several calls.")
        parts += [html[last:match.start(3)], f"data:{mime};base64,{base64.b64encode(data).decode()}"]
        last = match.end(3)
    parts.append(html[last:])
    return "".join(parts)


async def _picture_bytes(raw: str, session: Any, kwargs: dict[str, Any], *, svg: bool) -> tuple[bytes, str]:
    """A picture in the workspace, and its type: a PNG or JPEG, or with *svg* an SVG too."""
    path = relative_path(raw, session)
    try:
        async with session_files(session, storage=kwargs["storage"], session_factory=kwargs["session_factory"],
                                 redis=kwargs["redis"]) as files:
            data = await files.read(await files.resolve(path), max_bytes=MAX_IMAGE_BYTES + 1)
    except (FileNotFoundError, WorkspaceSandboxError, ValueError):  # ValueError: a NUL in the path
        raise AgentDocumentError(f"No picture at {path}.") from None
    except (ComputerAway, DeviceOperationError, OSError) as exc:
        raise AgentDocumentError(f"Could not read {path}: {said(exc)}") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise AgentDocumentError(f"{path} is over 5 MB; use a smaller picture.")
    kinds = "a PNG, JPEG or SVG" if svg else "a PNG or JPEG"
    if svg and path.lower().endswith(".svg"):
        if not data[:512].lstrip().lower().startswith((b"<svg", b"<?xml")):
            raise AgentDocumentError(f"{path} is not {kinds}.")
        # runtime-office checks what it holds, as it checks every SVG picture on a slide.
        return data, "image/svg+xml"
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = Image.MIME.get(image.format or "")
    except Exception:
        raise AgentDocumentError(f"{path} is not {kinds}.") from None
    if mime not in ("image/png", "image/jpeg"):
        raise AgentDocumentError(f"{path} is not {kinds}.")
    return data, mime


async def _picture(block: dict[str, Any], session: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    data, mime = await _picture_bytes(str(block.get("path") or ""), session, kwargs, svg=False)
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    shown = int(block.get("width") or width)
    return {"type": "image", "data": base64.b64encode(data).decode(), "mime": mime, "width": shown,
            "height": max(1, round(height * shown / width))}


def _read_as(notes: list[str]) -> dict[str, Any]:
    """What an answer says of the ops read differently from how the model sent them: nothing when nothing was."""
    return {"read_as": notes} if notes else {}


def _error(message: str, **reading: Any) -> str:
    """A failed call's answer; *reading* says what it read differently from how the model sent it."""
    return json.dumps({"error": message, **reading}, ensure_ascii=False)
