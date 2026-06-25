"""Derive inbox item fields from inbox-class session events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from surogates.session.events import EventType


@dataclass(frozen=True, slots=True)
class InboxRow:
    kind: str
    title: str
    body: str | None
    payload: dict[str, Any]
    action_ref: dict[str, Any] | None


# Inbox-item kinds that are purely informational: the operator acknowledges
# them (or just sees the result) rather than responding. They are therefore
# acknowledge-able, not auto-expired on terminal sessions, and suppressed while
# the session is actively viewed. Kinds not listed here need a real response.
ACKNOWLEDGE_ONLY_KINDS = frozenset({"task_complete", "progress_checkin"})

_TITLE_TRUNCATE = 120
_BODY_TRUNCATE = 1000


def _truncate(value: str | None, *, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def build_inbox_row(
    *,
    event_type: EventType,
    event_data: dict[str, Any],
    session_id: str,
) -> InboxRow | None:
    """Build the denormalized inbox row payload for recognized inbox events."""

    if event_type == EventType.INBOX_INPUT_REQUIRED:
        return _input_required(event_data, session_id)
    if event_type == EventType.INBOX_ACTION_REQUIRED:
        return _action_required(event_data, session_id)
    if event_type == EventType.INBOX_TASK_COMPLETE:
        return _task_complete(event_data)
    if event_type == EventType.INBOX_GOVERNANCE_GATE:
        return _governance_gate(event_data)
    if event_type == EventType.INBOX_PROGRESS_CHECKIN:
        return _progress_checkin(event_data)
    return None


def _input_required(data: dict[str, Any], session_id: str) -> InboxRow:
    tool_call_id = data["tool_call_id"]
    questions = data.get("questions") or []
    first_prompt = _first_question_prompt(questions)
    title = first_prompt or "Agent needs input"

    return InboxRow(
        kind="input_required",
        title=_truncate(title, limit=_TITLE_TRUNCATE),
        body=_truncate(data.get("context"), limit=_BODY_TRUNCATE) or None,
        payload={
            "tool_call_id": tool_call_id,
            "questions": questions,
            "context": data.get("context", ""),
        },
        action_ref={
            "type": "ask_user_question_response",
            "tool_call_id": tool_call_id,
            "endpoint": (
                f"/v1/sessions/{session_id}/ask_user_question/"
                f"{tool_call_id}/respond"
            ),
        },
    )


def _first_question_prompt(questions: list[Any]) -> str | None:
    if not questions or not isinstance(questions[0], dict):
        return None
    prompt = questions[0].get("prompt")
    return prompt if isinstance(prompt, str) else None


def _action_required(data: dict[str, Any], session_id: str) -> InboxRow:
    action_type = _truncate(data.get("action_type") or "manual", limit=80)
    target = _truncate(data.get("target") or "session", limit=80)
    instructions = _truncate(data.get("instructions"), limit=_BODY_TRUNCATE)
    context = _truncate(data.get("context"), limit=_BODY_TRUNCATE)
    body = instructions or context or None
    title = data.get("title") or _default_action_title(action_type)

    return InboxRow(
        kind="action_required",
        title=_truncate(title, limit=_TITLE_TRUNCATE),
        body=body,
        payload={
            "action_type": action_type,
            "target": target,
            "instructions": instructions,
            "context": context,
            "reason": data.get("reason", ""),
        },
        action_ref={
            "type": "open_session",
            "session_id": session_id,
            "target": target,
            "completion_endpoint": "/v1/inbox/{item_id}/respond",
        },
    )


def _default_action_title(action_type: str) -> str:
    if action_type == "browser":
        return "Browser action required"
    if action_type == "approval":
        return "Approval required"
    return "Action required"


def _task_complete(data: dict[str, Any]) -> InboxRow:
    title = data.get("session_title") or "Task complete"

    return InboxRow(
        kind="task_complete",
        title=_truncate(title, limit=_TITLE_TRUNCATE),
        body=_truncate(data.get("summary"), limit=_BODY_TRUNCATE) or None,
        payload={
            "outcome": data.get("outcome", "success"),
            "summary": data.get("summary", ""),
            "duration_seconds": int(data.get("duration_seconds", 0)),
            "error": data.get("error"),
        },
        action_ref=None,
    )


def _governance_gate(data: dict[str, Any]) -> InboxRow:
    tool_name = data["tool_name"]
    body = "\n\n".join(
        part
        for part in (
            data.get("deny_reason", ""),
            data.get("arguments_excerpt", ""),
        )
        if part
    )

    return InboxRow(
        kind="governance_gate",
        title=_truncate(
            f"Approval needed: {tool_name}",
            limit=_TITLE_TRUNCATE,
        ),
        body=_truncate(body, limit=_BODY_TRUNCATE) or None,
        payload={
            "tool_name": tool_name,
            "tool_call_id": data["tool_call_id"],
            "arguments_excerpt": data.get("arguments_excerpt", ""),
            "deny_reason": data.get("deny_reason", ""),
            "policy_id": data.get("policy_id"),
        },
        action_ref={
            "type": "governance_decision",
            "endpoint": "/v1/inbox/{item_id}/respond",
            "choices": ["approve", "reject"],
        },
    )


def _progress_checkin(data: dict[str, Any]) -> InboxRow:
    iterations = int(data.get("iterations", 0))
    elapsed_seconds = int(data.get("elapsed_seconds", 0))
    title = (
        f"Progress: {iterations} iterations, "
        f"{elapsed_seconds // 60} min elapsed"
    )

    return InboxRow(
        kind="progress_checkin",
        title=_truncate(title, limit=_TITLE_TRUNCATE),
        body=_truncate(
            data.get("progress_summary"),
            limit=_BODY_TRUNCATE,
        ) or None,
        payload={
            "progress_summary": data.get("progress_summary", ""),
            "iterations": iterations,
            "last_tool": data.get("last_tool", ""),
            "elapsed_seconds": elapsed_seconds,
        },
        action_ref=None,
    )
