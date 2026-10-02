"""Resume the tool calls a stopped worker left unanswered in a local-folder session.

A worker can stop after the computer ran an operation but before the call's
tool result was committed.  Seeing no result, the model could make the call
again and repeat its effect.  So a wake, before anything else, runs each
unanswered call of the latest model response that has operations in the
journal again under its original tool.call event: operations already
recorded return their outcomes, an open one is waited for, and the real tool
result is committed.  A call with no operations recorded did nothing on the
computer and is left to the usual "result unavailable" stub.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from surogates.db.models import DeviceOperation
from surogates.devices.binding import device_of
from surogates.session.events import EventType


def unanswered_calls(events: list[Any]) -> list[tuple[int, dict[str, Any]]]:
    """Each call of the latest model response with a tool.call and no tool.result, as (that event's id, the call).

    Oldest first.  Only the latest response's calls count: once the model has
    answered again, an earlier call is past resuming.  The call is the one
    the model's llm.response recorded, which ``emit_event`` stored redacted.
    """
    asked: dict[str, dict[str, Any]] = {}
    started: dict[str, int] = {}
    for event in events:
        kind = str(getattr(event.type, "value", event.type))
        data = event.data or {}
        if kind == EventType.LLM_RESPONSE.value:
            asked = {
                call["id"]: call
                for call in (data.get("message") or {}).get("tool_calls") or []
                if call.get("id")
            }
        elif kind == EventType.TOOL_CALL.value and data.get("tool_call_id"):
            started[data["tool_call_id"]] = event.id
        elif kind == EventType.TOOL_RESULT.value:
            started.pop(data.get("tool_call_id"), None)
    return sorted(
        ((event_id, asked[call_id]) for call_id, event_id in started.items() if call_id in asked),
        key=lambda item: item[0],
    )


def resumable(session: Any, events: list[Any]) -> bool:
    """Whether a local-folder session has calls a stopped worker left unanswered.

    That is work for a wake even when a sibling's result moved the harness
    cursor past them.
    """
    return device_of(session.config) is not None and bool(unanswered_calls(events))


def place(messages: list[dict[str, Any]], result: dict[str, Any]) -> None:
    """Put a tool result after the results already answering its assistant message."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "assistant" and any(
            call.get("id") == result["tool_call_id"] for call in message.get("tool_calls") or []
        ):
            end = index + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            messages.insert(end, result)
            return


async def _journaled(session_factory: Any, calling_session_id: Any, invocations: list[str]) -> set[str]:
    async with session_factory() as db:
        rows = await db.execute(
            select(DeviceOperation.invocation_id)
            .where(
                DeviceOperation.calling_session_id == calling_session_id,
                DeviceOperation.invocation_id.in_(invocations),
            )
            .distinct()
        )
    return set(rows.scalars())


async def replay_unanswered(
    *,
    session: Any,
    events: list[Any],
    messages: list[dict[str, Any]],
    session_factory: Any,
    run_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> None:
    """Resume each unanswered call with operations in the journal, adding its result to *messages*."""
    calls = {f"{event_id}:{call['id']}": (event_id, call) for event_id, call in unanswered_calls(events)}
    if not calls:
        return
    journaled = await _journaled(session_factory, session.id, list(calls))
    for invocation, (event_id, call) in calls.items():
        if invocation in journaled:
            place(messages, await run_tool({**call, "_replay_of": event_id}))
