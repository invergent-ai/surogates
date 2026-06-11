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
from surogates.board.types import NOTE_TYPES
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
        "Share compact, verified notes on your coordination group's board "
        "so parallel workers (and your coordinator) can reuse them. Types: "
        "FACT (concrete reusable knowledge, anchored to a file/symbol/"
        "endpoint/error), FAIL (a dead end you actually hit and why — the "
        "highest-value note for peers), CLAIM (short-lived 'I am working on "
        "X' to prevent overlap; expires automatically), RESULT (your "
        "candidate outcome as `outcome=…|evidence=…|risk=…` where evidence "
        "names a check you ACTUALLY ran and its observed result). Notes are "
        "admitted only after verification — vague or unevidenced notes are "
        "rejected with a reason. Batch related notes into one call."
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
                                "Optional pointer to expandable detail: "
                                "{kind:'event', session_id, event_id} or "
                                "{kind:'artifact', session_id, artifact_id}."
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
        return await verify_notes_llm(
            drafts,
            llm_client=verifier_client,
            model=verifier_model,
            timeout_seconds=_VERIFIER_TIMEOUT_SECONDS,
        )

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


def register(registry: ToolRegistry) -> None:
    """Register board tools. Called once per registry by tools/runtime.py."""
    registry.register(
        name="share_note",
        schema=_SHARE_NOTE_SCHEMA,
        handler=_share_note_handler,
        toolset="core",
    )
