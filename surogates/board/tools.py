"""Coordination-board tools: share_note, read_board, expand_note.

Visibility is gated on ``session.config['context_group_id']`` (see
``_filter_effective_tools``).  All handlers double-check membership and
tenancy themselves — tool-schema gating is UX, not security.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.board.render import render_board
from surogates.board.store import BoardStore
from surogates.board.types import CITED_TYPES, NOTE_TYPES
from surogates.board.verifier import NoteDraft, verify_notes_llm
from surogates.config import get_board_settings
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

_VERIFIER_TIMEOUT_SECONDS = 25.0

BOARD_TOOLS: frozenset[str] = frozenset(
    {"share_note", "read_board", "expand_note"}
)

_SHARE_NOTE_SCHEMA = ToolSchema(
    name="share_note",
    description=(
        "Share compact notes on your coordination group's board so parallel "
        "workers (and your coordinator) can reuse them. Types: FACT "
        "(concrete reusable knowledge, anchored to a file/symbol/endpoint/"
        "error), FAIL (a dead end you actually hit and why: the "
        "highest-value note for peers), CLAIM (short-lived 'I am working on "
        "X' to prevent overlap; expires automatically), RESULT (your "
        "candidate outcome as `outcome=…|evidence=…|risk=…`). FACT, FAIL and "
        "RESULT must cite their source in `ref`, usually the tool result "
        "that shows it; a RESULT cites a check from your own session. Each "
        "note is admitted only if a verifier finds the cited source supports "
        "it; otherwise it is rejected with a reason. Batch related notes "
        "into one call."
    ),
    parameters={
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "description": "Notes to admit (batched).",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(NOTE_TYPES)},
                        "content": {
                            "type": "string",
                            "description": (
                                "≤200 chars (FACT/FAIL/CLAIM) or ≤400 "
                                "(RESULT). Specific and self-contained."
                            ),
                        },
                        "ref": {
                            "type": "object",
                            "description": (
                                "The source the note cites (required for "
                                "FACT, FAIL and RESULT): {kind:'tool_result', "
                                "tool:'<tool name>', contains:'<text from its "
                                "output>'} for your own latest matching tool "
                                "result, or {kind:'event', session_id, "
                                "event_id}, or {kind:'artifact', session_id, "
                                "artifact_id}. It must be in your group."
                            ),
                        },
                    },
                    "required": ["type", "content"],
                    "additionalProperties": False,
                },
            },
            "ttl_seconds": {
                "type": "integer",
                "description": (
                    "Optional CLAIM lifetime override (default 300)."
                ),
            },
        },
        "required": ["notes"],
        "additionalProperties": False,
    },
)


def _group_id_or_none(session_config: dict | None) -> UUID | None:
    raw = (session_config or {}).get("context_group_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


def _writer_label(session_id: UUID, group_id: UUID) -> str:
    return "coord" if session_id == group_id else f"w{session_id.hex[:4]}"


_SOURCE_MAX_CHARS = 1500


async def _tool_result_ref(
    ref: dict[str, Any], *, session_factory: Any, writer_session_id: UUID,
) -> tuple[dict[str, Any] | None, str | None]:
    """The writer's own latest result of ``ref["tool"]`` (whose output
    contains ``ref["contains"]`` when given), as a concrete event ref.

    A model sees its tool calls by name and output, never by event id, so
    this is the ref it can write; the note keeps the event it resolved to.
    """
    from sqlalchemy import select

    from surogates.db.models import Event

    tool = str(ref.get("tool") or "").strip()
    if not tool:
        return None, "a tool_result ref needs the tool's name"
    stmt = (
        select(Event.id)
        .where(Event.session_id == writer_session_id)
        .where(Event.type == EventType.TOOL_RESULT.value)
        .where(Event.data["name"].astext == tool)
        .order_by(Event.id.desc())
        .limit(1)
    )
    contains = str(ref.get("contains") or "").strip()
    if contains:
        stmt = stmt.where(Event.data["content"].astext.contains(contains))
    async with session_factory() as db:
        event_id = (await db.execute(stmt)).scalar_one_or_none()
    if event_id is None:
        return None, (
            f"no result of {tool} in your session"
            + (f" contains {contains!r}" if contains else "")
        )
    return {"kind": "event", "session_id": str(writer_session_id), "event_id": event_id}, None


async def _cite(
    draft: NoteDraft, *, writer_session_id: UUID, group_id: UUID, kwargs: dict[str, Any],
) -> str | None:
    """Resolve and read the source *draft* cites, so the verifier judges it
    against that source.  Fills ``draft.ref`` (concrete) and
    ``draft.source``; returns why it cannot be admitted, else None."""
    if draft.type not in CITED_TYPES:
        return None
    if not draft.ref:
        return f"{draft.type} needs a ref to the tool result, event or artifact that shows it"
    ref = draft.ref
    if ref.get("kind") == "tool_result":
        resolved, error = await _tool_result_ref(
            ref, session_factory=kwargs["session_factory"],
            writer_session_id=writer_session_id,
        )
        if resolved is None:
            return f"ref: {error}"
        ref = resolved
    if draft.type == "RESULT" and str(ref.get("session_id")) != str(writer_session_id):
        return "a RESULT must cite a check from your own session"
    detail, error = await _read_ref(
        ref, group_id=group_id, max_chars=_SOURCE_MAX_CHARS, kwargs=kwargs,
    )
    if error is not None:
        return f"ref: {error}"
    if not detail.strip():
        return "ref: the cited source is empty"
    draft.ref, draft.source = ref, detail
    return None


async def _share_note_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    session_factory = kwargs["session_factory"]
    session_store = kwargs["session_store"]
    tenant = kwargs["tenant"]
    session_id = UUID(str(kwargs["session_id"]))
    session_config = kwargs.get("session_config") or {}

    group_id = _group_id_or_none(session_config)
    if group_id is None:
        return json.dumps({
            "error": (
                "share_note is only available inside a coordination group "
                "(no context_group_id on this session)."
            ),
        })

    raw_notes = arguments.get("notes")
    if not isinstance(raw_notes, list) or not raw_notes:
        return json.dumps({"error": "notes must be a non-empty array"})

    settings = get_board_settings()
    ttl = int(arguments.get("ttl_seconds") or settings.claim_ttl_seconds)
    ttl = max(30, min(ttl, 3600))

    verifier_client = kwargs.get("summary_llm_client") or kwargs.get("llm_client")
    verifier_model = kwargs.get("summary_model") or kwargs.get("model")
    if verifier_client is None or not verifier_model:
        # Fail-closed: no verifier available means nothing is admitted.
        return json.dumps({
            "admitted": [],
            "renewed_claims": [],
            "rejected": [
                {"index": i, "reason":
                 "verification unavailable — retry on a later turn"}
                for i in range(len(raw_notes))
            ],
        })

    async def _verifier(drafts: list[NoteDraft]):
        cited: list[int] = []
        refused: list[tuple[int, str]] = []
        for i, draft in enumerate(drafts):
            reason = await _cite(
                draft, writer_session_id=session_id, group_id=group_id, kwargs=kwargs,
            )
            if reason is None:
                cited.append(i)
            else:
                refused.append((i, reason))
        kept, rejected = await verify_notes_llm(
            [drafts[i] for i in cited],
            llm_client=verifier_client,
            model=verifier_model,
            timeout_seconds=_VERIFIER_TIMEOUT_SECONDS,
        )
        return kept, refused + [(cited[j], reason) for j, reason in rejected]

    board = BoardStore(session_factory)
    result = await board.admit(
        raw_notes=raw_notes,
        org_id=tenant.org_id,
        group_id=group_id,
        writer_session_id=session_id,
        writer_label=_writer_label(session_id, group_id),
        verifier=_verifier,
        max_claims_per_writer=settings.max_active_claims_per_writer,
        max_notes_per_group=settings.max_notes_per_group,
        claim_ttl_seconds=ttl,
    )

    if result.admitted:
        await session_store.emit_event(
            session_id,
            EventType.BOARD_NOTE,
            {
                "group_id": str(group_id),
                "notes": [
                    {"id": n.id, "type": n.type, "content": n.content}
                    for n in result.admitted
                ],
            },
        )

    return json.dumps({
        "admitted": [
            {"id": n.id, "type": n.type, "writer_label": n.writer_label}
            for n in result.admitted
        ],
        "renewed_claims": result.renewed,
        "rejected": [
            {"index": idx, "reason": reason}
            for idx, reason in result.rejected
        ],
    })


_READ_BOARD_SCHEMA = ToolSchema(
    name="read_board",
    description=(
        "Read your coordination group's board: the consolidated CURRENT "
        "state (superseded results and expired claims already removed). "
        "Use at decision points — before committing to an approach, or "
        "when planning follow-up work — since inline [Board update] "
        "messages in your history may be stale."
    ),
    parameters={
        "type": "object",
        "properties": {
            "types": {
                "type": "array",
                "items": {"type": "string", "enum": list(NOTE_TYPES)},
                "description": "Optional filter to these note types.",
            },
        },
        "additionalProperties": False,
    },
)

_EXPAND_NOTE_SCHEMA = ToolSchema(
    name="expand_note",
    description=(
        "Expand a board note (by its n<ID> number) into the underlying "
        "detail behind its ref: the source event content or artifact "
        "payload, bounded to 4000 chars. Errors if the note has no ref."
    ),
    parameters={
        "type": "object",
        "properties": {
            "note_id": {"type": "integer", "description": "Numeric note id."},
        },
        "required": ["note_id"],
        "additionalProperties": False,
    },
)

_EXPAND_MAX_CHARS = 4000


async def _read_board_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    session_store = kwargs["session_store"]
    session_factory = kwargs["session_factory"]
    session_id = UUID(str(kwargs["session_id"]))
    group_id = _group_id_or_none(kwargs.get("session_config"))
    if group_id is None:
        return json.dumps({"error": "not a coordination-group member"})

    settings = get_board_settings()
    board = BoardStore(session_factory)
    notes = await board.active_notes(group_id)

    types = arguments.get("types")
    if types:
        wanted = {str(t).upper() for t in types}
        notes = [n for n in notes if n.type in wanted]

    text = render_board(
        notes,
        max_tokens=settings.read_tool_window_tokens,
        now=datetime.now(timezone.utc),
        header="[Board — consolidated current state]",
        footer="",
    ) or "(board is empty)"

    # Any durable render advances the persisted cursor (spec §7).  The
    # in-wake loop cursor may lag until next wake; the resulting overlap
    # is one small repeated delta, which is harmless.
    max_seq = await board.max_seq(group_id)
    if max_seq:
        await session_store.update_session_config_key(
            session_id, "board_cursor", max_seq,
        )
    return text


def _extract_event_text(data: dict[str, Any]) -> str:
    content = data.get("content")
    if isinstance(content, str) and content:
        return content
    message = data.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return json.dumps(data)


async def _check_ref_target(
    *,
    session_store: Any,
    tenant: Any,
    target_sid: UUID,
    group_id: UUID,
) -> Any | None:
    """Load the ref target session iff it is accessible from this group.

    Confinement rule: the target must be the group root itself or carry
    the same ``context_group_id`` — refs must not become a side door
    into arbitrary org sessions.  Returns the session row or None.
    """
    from surogates.session.store import SessionNotFoundError

    try:
        target = await session_store.get_session(target_sid)
    except SessionNotFoundError:
        return None
    if target is None or target.org_id != tenant.org_id:
        return None
    target_group = (target.config or {}).get("context_group_id")
    if str(target_sid) != str(group_id) and target_group != str(group_id):
        return None
    return target


async def _read_ref(
    ref: dict[str, Any], *, group_id: UUID, max_chars: int, kwargs: dict[str, Any],
) -> tuple[str, str | None]:
    """What *ref* points at, bounded to *max_chars*: ``(detail, None)``, or
    ``("", why not)`` when it is malformed, outside the group or gone.

    The one reader for refs: ``expand_note`` shows what it returns, and
    ``share_note`` hands it to the verifier as the note's source.
    """
    session_store = kwargs["session_store"]
    tenant = kwargs["tenant"]
    kind = str(ref.get("kind") or "")
    if kind == "event":
        try:
            target_sid = UUID(str(ref.get("session_id")))
            event_id = int(ref.get("event_id"))
        except (TypeError, ValueError):
            return "", "malformed event ref"
        target = await _check_ref_target(
            session_store=session_store, tenant=tenant,
            target_sid=target_sid, group_id=group_id,
        )
        if target is None:
            return "", "ref target not accessible"
        event = await session_store.get_event_by_id(target_sid, event_id)
        if event is None:
            return "", "ref event not found"
        return _extract_event_text(event.data or {})[:max_chars], None

    if kind == "artifact":
        try:
            target_sid = UUID(str(ref.get("session_id")))
            artifact_id = UUID(str(ref.get("artifact_id")))
        except (TypeError, ValueError):
            return "", "malformed artifact ref"
        target = await _check_ref_target(
            session_store=session_store, tenant=tenant,
            target_sid=target_sid, group_id=group_id,
        )
        if target is None:
            return "", "ref target not accessible"
        from surogates.artifacts.store import (
            ArtifactNotFoundError,
            ArtifactStore,
        )
        from surogates.devices.binding import device_of
        from surogates.devices.workspace import DeviceOperationError
        from surogates.sandbox.pool import sandbox_session_key
        from surogates.session.attachment_ingest import workspace_root_id
        from surogates.storage.tenant import boundary_workspace_prefix
        from surogates.tools.workspace_io import StorageWorkspaceIO

        if device_of(target.config) is not None:
            # On a local folder: read through this tool call, and only on
            # the same folder, the one its computer gives this call.
            files = kwargs.get("workspace_io")
            if files is None or sandbox_session_key(target) != kwargs.get("task_id"):
                return "", "ref artifact is on a local folder this session cannot reach"
        else:
            storage = kwargs.get("storage")
            bucket = (target.config or {}).get("storage_bucket")
            if storage is None or not bucket:
                return "", "artifact storage unavailable"
            files = StorageWorkspaceIO(
                storage,
                bucket=bucket,
                prefix=boundary_workspace_prefix(target.config, target, workspace_root_id(target)),
            )
        artifact_store = ArtifactStore(files, session_id=target_sid, root=sandbox_session_key(target))
        try:
            payload = await artifact_store.get_payload(artifact_id)
        except ArtifactNotFoundError:
            return "", "ref artifact not found"
        except DeviceOperationError as exc:
            # A local folder's computer that will not read it, in its own words.
            return "", f"ref artifact could not be read: {exc}"
        return json.dumps(payload)[:max_chars], None

    return "", f"unknown ref kind {kind!r}"


async def _expand_note_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    session_factory = kwargs["session_factory"]
    tenant = kwargs["tenant"]
    group_id = _group_id_or_none(kwargs.get("session_config"))
    if group_id is None:
        return json.dumps({"error": "not a coordination-group member"})

    note_id = arguments.get("note_id")
    if not isinstance(note_id, int):
        return json.dumps({"error": "note_id must be an integer"})

    board = BoardStore(session_factory)
    note = await board.get_note(note_id)
    if note is None or note.group_id != group_id or note.org_id != tenant.org_id:
        return json.dumps({"error": f"note n{note_id} not found on your board"})
    if not note.ref:
        return json.dumps({"error": "note has no expandable detail"})

    detail, error = await _read_ref(
        note.ref, group_id=group_id, max_chars=_EXPAND_MAX_CHARS, kwargs=kwargs,
    )
    if error is not None:
        return json.dumps({"error": error})
    return json.dumps(
        {"note_id": note_id, "kind": str(note.ref.get("kind")), "detail": detail}
    )


def register(registry: ToolRegistry) -> None:
    """Register board tools. Called once per registry by tools/runtime.py."""
    registry.register(
        name="share_note",
        schema=_SHARE_NOTE_SCHEMA,
        handler=_share_note_handler,
        toolset="core",
    )
    registry.register(
        name="read_board",
        schema=_READ_BOARD_SCHEMA,
        handler=_read_board_handler,
        toolset="core",
    )
    registry.register(
        name="expand_note",
        schema=_EXPAND_NOTE_SCHEMA,
        handler=_expand_note_handler,
        toolset="core",
    )
