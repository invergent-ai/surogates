"""Ask a session about its task list when the list has gone stale.

A model that keeps a task list tends to write it twice: the plan at the start,
and every item marked done in one call just before it finishes. In between, the
people who watch the list -- a case's page, a chat -- see a plan that does not
move for as long as the work takes.

The reminder is a nudge, not a rule. It fires only for a session that has a
list with items still open, and only after several tool-calling iterations
without a ``todo`` call; a session with no list is not pushed to make one. It
is added to the last tool result of the iteration, in memory, as the budget
warning is: it is never an event, so it shows in no chat and no log, and it is
stripped from replayed messages.

No model call: the check reads the list the session already wrote.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: Tool-calling iterations without a ``todo`` call after which a list with open
#: items is asked about. Past it the check repeats every this many iterations.
TODO_REMINDER_AFTER = 6

#: The key a reminder takes in a JSON tool result, and the marker it starts
#: with in a plain-text one; replay strips both.
TODO_REMINDER_KEY = "_todo_reminder"
TODO_REMINDER_MARKER = "[TASK LIST REMINDER]"

TODO_REMINDER = (
    f"{TODO_REMINDER_MARKER} Your task list has not changed for several steps. "
    "If you have finished an item, mark it completed and set the next one "
    "in_progress now, with one todo call (merge=true). If the plan changed, "
    "revise it. Then carry on."
)

#: The statuses of an item that is not done.
_OPEN = frozenset({"pending", "in_progress"})

_REMINDER_TEXT_RE = re.compile(rf"\n*{re.escape(TODO_REMINDER_MARKER)}[^\n]*")


def iterations_since_todo(since: int, tool_calls: list[dict[str, Any]]) -> int:
    """How many tool-calling iterations the session has gone without a ``todo`` call.

    *since* is the count before this iteration; a ``todo`` call among
    *tool_calls* brings it to zero.
    """
    for call in tool_calls:
        if call.get("function", {}).get("name") == "todo":
            return 0
    return since + 1


async def stale_todo_reminder(store: Any, session_id: UUID) -> str | None:
    """The reminder for *session_id*'s task list, or ``None`` when it needs none.

    ``None`` for a session that never wrote a list, whose list has no item
    left open, or whose list cannot be read now: a reminder is never worth
    failing a turn.
    """
    try:
        todos = await store.latest_todo_snapshot(session_id)
    except Exception:
        logger.debug("todo reminder: list of session %s unreadable", session_id, exc_info=True)
        return None
    if not todos:
        return None
    if any(isinstance(item, dict) and item.get("status") in _OPEN for item in todos):
        return TODO_REMINDER
    return None


def inject_todo_reminder(tool_results: list[dict], reminder: str) -> list[dict]:
    """Add *reminder* to the last tool result: a key in a JSON object, else a line of text."""
    if not tool_results:
        return tool_results
    last = tool_results[-1]
    content = last.get("content", "")
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        parsed[TODO_REMINDER_KEY] = reminder
        content = json.dumps(parsed, ensure_ascii=False)
    else:
        content = f"{content}\n\n{reminder}"
    tool_results[-1] = {**last, "content": content}
    return tool_results


def strip_todo_reminders(messages: list[dict]) -> list[dict]:
    """Remove reminders from the tool results of replayed *messages*: they are turn-scoped."""
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content", "")
        if not isinstance(content, str):
            continue
        if TODO_REMINDER_KEY not in content and TODO_REMINDER_MARKER not in content:
            continue
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and TODO_REMINDER_KEY in parsed:
            del parsed[TODO_REMINDER_KEY]
            message["content"] = json.dumps(parsed, ensure_ascii=False)
            continue
        cleaned = _REMINDER_TEXT_RE.sub("", content).strip()
        if cleaned != content:
            message["content"] = cleaned
    return messages
