"""office_read and office_edit: the agent reads and edits a Word document through its own live editor session.

The document is the one the user may have open in the chat's file pane: each office_edit call appears there whole,
under the agent's name. runtime-office holds the agent's editor page; this module opens it and passes the call on.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from PIL import Image

from surogates.config import load_settings
from surogates.office.agent import AgentDocument, AgentDocumentError, open_for_agent, relative_path
from surogates.office.runtime_office import OfficeBusy, PageGone, RuntimeOffice, RuntimeOfficeError
from surogates.session.files import session_files
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_NAME_TTL_S = 300.0
# The agent's display name by agent id, read from its runtime config at most every five minutes.
_names: dict[str, tuple[float, str]] = {}

READ_DESCRIPTION = (
    "Read a Word document (.docx) in the workspace through your own live editor session: the same document the user "
    "may have open in the file pane, with everyone's latest edits. Read before every office_edit: the outline gives "
    "each block's id and hash, which office_edit targets.\n"
    'view "outline" (default) lists the blocks in order (id, hash, kind, first words), with comment threads and '
    'tracked changes; a long document comes in pages, continue with start=<next>. view "block" shows one block in full '
    '(target = its id; a table\'s cells). view "search" finds words (query) and the blocks they are in.\n'
    "The result says who else has the document open."
)

EDIT_DESCRIPTION = (
    "Edit a Word document (.docx) live through your own editor session. The user watches each call's changes appear in "
    "the file pane under your name, and may be typing in the same document. Make one piece of work per call (a "
    "section, a list, a table) and say a short line in the chat between calls. A missing document is created blank "
    "and opens in the user's pane. While a document is open here, change it only with this tool, never with "
    "write_file, patch or terminal.\n"
    "All ops of a call apply together or not at all. If a block you target changed since you read it, or quoted words "
    "are gone, nothing applies and the result shows the block as it is now: the user's edit wins; read again and work "
    "from it.\n"
    'Targets: {"block": "<id>", "hash": "<hash>"} from office_read; {"find": "exact words", "nth": 1}; '
    '{"pending": "<key>"}; {"end": true} (insert only).\n'
    "Ops: insert {target, side: before|after, content}; replace {target, content}: a block or pending block with "
    'markdown content, or quoted words with {"text": "…"} content to re-word them in place keeping their formatting; '
    "delete {target}; move {target, to, side} (a moved block loses the comments anchored in it); "
    "comment {anchor: {find}, body}; reply {thread, body}; resolve {thread}.\n"
    'Content: {"markdown": "…", "blocks": {…}}. Headings use the document\'s Heading 1-3, lists its numbering, tables its '
    "table style. A line <?block k?> places blocks[k]: "
    '{"type": "pending", "intent": "what this section will say"} (a placeholder the user sees until you replace it: '
    "plant a new document's sections as pending blocks first, then fill one per call), "
    '{"type": "chart", "chart": "bar|column|horizontal-bar|line|pie|area|doughnut|scatter", "title": "…", '
    '"rows": [["", "Series A"], ["Category", 1]]}, or {"type": "image", "path": "<PNG or JPEG in the workspace>", '
    '"width": <px>}.'
)

READ_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The document's path in the workspace."},
        "view": {"type": "string", "enum": ["outline", "block", "search"]},
        "target": {"type": "string", "description": 'view "block": the block id.'},
        "query": {"type": "string", "description": 'view "search": the words to find.'},
        "start": {"type": "integer", "description": 'view "outline": the block to start at (the previous page\'s next).'},
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
    elif view == "block" and arguments.get("target"):
        request = {"view": "block", "target": str(arguments["target"])}
    elif view == "search" and arguments.get("query"):
        request = {"view": "search", "query": str(arguments["query"])}
    else:
        return _error('view is "outline", "block" (with target) or "search" (with query).')

    async def call(runtime: RuntimeOffice, document: AgentDocument, session: Any) -> dict[str, Any]:
        return await runtime.read(document.page_id, request)

    return await _run(arguments, kwargs, create=False, call=call)


async def _office_edit(arguments: dict[str, Any], **kwargs: Any) -> str:
    ops = arguments.get("ops")
    if not isinstance(ops, list) or not ops:
        return _error('"ops" is a non-empty list of ops.')

    async def call(runtime: RuntimeOffice, document: AgentDocument, session: Any) -> dict[str, Any]:
        return await runtime.edit(document.page_id, await _with_pictures(ops, session, kwargs))

    return await _run(arguments, kwargs, create=True, call=call)


async def _run(arguments: dict[str, Any], kwargs: dict[str, Any], *, create: bool, call: Call) -> str:
    office = load_settings().office
    raw_path = str(arguments.get("path") or "").strip()
    if not raw_path:
        return _error('"path" names the document in the workspace.')
    session = await kwargs["session_store"].get_session(UUID(str(kwargs["session_id"])))
    path = relative_path(raw_path, session)
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
        return _error(str(exc))
    except OfficeBusy:
        logger.warning("runtime-office is full: %s in session %s", path, session.id)
        return _error("The office service has as many documents open as it can hold; try again in a minute.")
    except RuntimeOfficeError as exc:
        if exc.status == 400:
            return _error(str(exc))
        logger.warning("runtime-office failed on %s", path, exc_info=True)
        return _error(f"The editor could not do that: {exc}")
    answer: dict[str, Any] = {"document": path, "open_by": _open_by(document)}
    if document.created:
        answer["created"] = True
    if result.get("flushed") is False:
        answer["note"] = "The edit applied in your editor but had not reached the others' editors after 10 seconds."
    return json.dumps({**answer, **result}, ensure_ascii=False)


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
    """*ops* with every picture block's workspace path read into the bytes the editor inserts."""
    out: list[Any] = []
    for op in ops:
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


async def _picture(block: dict[str, Any], session: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    path = relative_path(str(block.get("path") or ""), session)
    async with session_files(session, storage=kwargs["storage"], session_factory=kwargs["session_factory"],
                             redis=kwargs["redis"]) as files:
        try:
            data = await files.read(await files.resolve(path), max_bytes=MAX_IMAGE_BYTES + 1)
        except (FileNotFoundError, WorkspaceSandboxError, ValueError):  # ValueError: a NUL in the path
            raise AgentDocumentError(f"No picture at {path}.") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise AgentDocumentError(f"{path} is over 5 MB; use a smaller picture.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = Image.MIME.get(image.format or "")
            width, height = image.size
    except Exception:
        raise AgentDocumentError(f"{path} is not a picture.") from None
    if mime not in ("image/png", "image/jpeg"):
        raise AgentDocumentError(f"{path} is not a PNG or JPEG.")
    shown = int(block.get("width") or width)
    return {"type": "image", "data": base64.b64encode(data).decode(), "mime": mime, "width": shown,
            "height": max(1, round(height * shown / width))}


def _error(message: str) -> str:
    return json.dumps({"error": message})
