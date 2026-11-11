"""What a session hears of background work it started: ``agent.notification``.

A background command the session started with ``notify_on_complete`` ends
long after the call that started it returned.  Its end is written to the
session as an ``agent.notification`` event and wakes it.  The session reads
the event as news (``loop_context_replay.news``): one user-role message at
its next model request, in the live loop and in replay alike, so a request
rebuilt from the log is the request that was sent.  A session at rest is
woken by it, and the notification is the input of the turn it gets.

Every message is built from the event's payload alone: the live loop and
replay must produce the same bytes.
"""

from __future__ import annotations

import json
import re
from typing import Any

from surogates.session.events import EventType

#: The events a session hears of its own background work by.
AGENT_NEWS_TYPES = frozenset({EventType.AGENT_NOTIFICATION.value})

#: The most characters of a background command's last output a notification carries.
MAX_RESULT_CHARS = 10_000


def _type(event: Any) -> str:
    kind = getattr(event, "type", None)
    return kind.value if isinstance(kind, EventType) else str(kind)


def notification_note(data: dict) -> dict:
    """The user-role message the session reads an ``agent.notification`` as.

    The harness speaks in its first line, in brackets; what the work printed
    or reported follows it, as a worker's report does.
    """
    data = data if isinstance(data, dict) else {}
    work = str(data.get("agent_id") or "?")
    status = data.get("status")
    result = str(data.get("result") or "")
    if data.get("kind") == "process":
        command = str(data.get("summary") or "")
        reason = data.get("reason")
        why = f" ({reason})" if reason else ""
        code = data.get("exit_code")
        if status == "lost":
            head = f"[Background process {work} cannot be watched any more: {command}{why}]"
        elif status == "killed" and code is None:
            head = f"[Background process {work} was stopped before it finished: {command}{why}]"
        else:
            said = "an unknown exit code" if code is None else f"exit code {code}"
            head = f"[Background process {work} finished with {said}: {command}]"
        # What it printed is the command's, not the harness's or the user's words: fenced off as its output.
        return {"role": "user", "content": f"{head}\nIts last output:\n{fenced(result)}" if result else head}
    if data.get("kind") == "monitor":
        named = f"Monitor {work} ({data.get('summary')})" if data.get("summary") else f"Monitor {work}"
        count, dropped = data.get("lines") or 0, data.get("dropped") or 0
        left_out = f"; {dropped} more line{'s' if dropped != 1 else ''} left out" if dropped else ""
        code, reason = data.get("exit_code"), data.get("reason")
        if status == "output":
            head = f"[{named}: {count} new line{'s' if count != 1 else ''}{left_out}]"
        elif status == "lost":
            head = f"[{named} cannot be watched any more{f' ({reason})' if reason else ''}{left_out}]"
        elif code is not None:
            head = f"[{named} ended: its command exited with code {code}{left_out}]"
        else:
            head = f"[{named} stopped{f': {reason}' if reason else ''}{left_out}]"
        # Its lines are the command's output, fenced off as such.
        return {"role": "user", "content": f"{head}\n{fenced(result)}" if result else head}
    if data.get("kind") == "agent":
        summary = data.get("summary")
        named = f"Agent {work} ({summary})" if summary else f"Agent {work}"
        if status == "failed":
            head = f"[{named} failed: {data.get('error') or 'unknown error'}]"
        elif status == "killed":
            head = f"[{named} was stopped before it finished]"
        elif data.get("reason"):
            head = f"[{named} ended before it finished ({data['reason']})]"
        else:
            head = f"[{named} completed]"
        return {"role": "user", "content": f"{head}\n{result}" if result else head}
    # Any other kind: its status and summary, in the harness's words.
    summary = data.get("summary")
    named = f"{work} ({summary})" if summary else work
    head = f"[{named} {status or 'ended'}]"
    return {"role": "user", "content": f"{head}\n{result}" if result else head}


def fenced(text: str) -> str:
    """*text* in a code fence longer than any run of backticks in it: what it holds cannot end it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def agent_news_unread(events: list) -> bool:
    """Whether a notification waits in *events* that no model request has read: one after the last ``llm.request``.

    The harness cursor cannot tell.  A notification that lands while a turn
    writes its last answer is behind the cursor once that turn ends, and the
    next request reads it all the same (``unread_reports``).
    """
    unread = False
    for event in events:
        kind = _type(event)
        if kind == EventType.LLM_REQUEST.value:
            unread = False
        elif kind in AGENT_NEWS_TYPES:
            unread = True
    return unread


def started_notified_work(events: list) -> bool:
    """Whether *events* hold a call that started background work which reports back:
    ``terminal`` with ``background`` and ``notify_on_complete``, or a notification already.

    Such a session reads its notifications live, before each model request
    (``AgentHarness._collect_reports``); any other session never gets one
    and is spared the read.
    """
    for event in events:
        kind = _type(event)
        if kind in AGENT_NEWS_TYPES:
            return True
        if kind == EventType.TOOL_CALL.value and starts_notified_work(
            (event.data or {}).get("name"), (event.data or {}).get("arguments"),
        ):
            return True
    return False


def starts_notified_work(name: Any, arguments: Any) -> bool:
    """Whether a call of tool *name* with *arguments* starts background work that reports back when it ends:
    a ``terminal`` command with ``background`` and ``notify_on_complete``, or an ``agent`` in the background."""
    if name == "monitor":
        return True
    if name not in ("terminal", "agent"):
        return False
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return False
    if not isinstance(arguments, dict):
        return False
    if name == "agent":
        return arguments.get("run_in_background") is True
    return bool(arguments.get("background")) and bool(arguments.get("notify_on_complete"))
