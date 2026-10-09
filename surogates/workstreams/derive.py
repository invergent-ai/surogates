"""A project thread's row: where it stands, derived when it is read.

Never stored: every input is already durable, and a stored state would drift
from the event log.  The row is the shell's ``ThreadRow``
(``web/src/lib/projects-contract.d.ts``) in snake_case, as the routes answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from surogates.session.events import MESSAGE_TYPES, EventType

#: ``ThreadGroup``, in the order the shell draws its sections.
GROUPS = ("waiting", "working", "idle", "resolved")
#: ``ThreadRow.reason``, when there is one.
REASONS = ("question", "approval", "failed", "computer", "files")
#: The inbox kinds a thread waits on the user for, and the reason each gives.
WAITING_KINDS = {"input_required": "question", "action_required": "approval", "governance_gate": "approval"}
#: The event types the rules read the latest of.  Turn summaries are read
#: too: every one for a row's files, or only the newest when the files are
#: not read (``WorkstreamStore.thread_facts``).
LATEST_TYPES = tuple(t.value for t in (
    *MESSAGE_TYPES, EventType.LLM_RESPONSE, EventType.TODO_UPDATED,
    EventType.ITERATION_SUMMARY, EventType.SESSION_FAIL, EventType.DEVICE_WAITING,
    EventType.DEVICE_RESUMED, EventType.HARNESS_WAKE,
))
#: A thread with no activity for this long counts as resolved.
QUIET_RESOLVES_AFTER = timedelta(days=7)
#: What the shell takes of a project's answers (``desktop/src/shell/projects.ts``,
#: which checks them against ``projects-contract.d.ts``): it refuses an answer
#: with anything past one of these, and every row in it.  Lengths are UTF-16
#: units, as the shell counts them.  A row's other fields stay inside the
#: shell's limits by construction: a title is at most 256 units, a status line
#: 200 code points.  A Library entry's path is a ``ref``.
SHELL_LIMITS = {"rows": 500, "files": 200, "label": 500, "ref": 4096, "library": 2000}
_STATUS_LINE_MAX = 200


@dataclass(frozen=True)
class ThreadFacts:
    """What a thread's row is derived from, as the store reads it."""

    id: UUID
    title: str
    status: str  # sessions.status
    created_at: datetime
    updated_at: datetime  # sessions.updated_at: naive, in UTC
    resolved_at: datetime | None
    place: dict[str, Any]
    #: Its inbox items of ``WAITING_KINDS`` that are pending or expired.
    items: tuple[Any, ...]
    #: Its latest event of each of ``LATEST_TYPES``, and its turn summaries:
    #: every one, or only the newest when its files are not read.
    events: tuple[Any, ...]
    #: Its landings that recorded files, each ``{id, files}`` as its
    #: ``workstream_history`` row has them; none when its files are not read.
    landings: tuple[dict[str, Any], ...] = ()
    #: The files its next turn is told to redo (``landing.redo_files``).
    redoing: frozenset[str] = frozenset()


def derive_thread(facts: ThreadFacts, *, now: datetime) -> dict[str, Any]:
    latest = _latest(facts)
    group, reason, status_line = _state(facts, latest, now)
    todos = latest.get(EventType.TODO_UPDATED.value)
    return {
        "id": str(facts.id),
        "title": facts.title,
        "group": group,
        "reason": reason,
        "status_line": status_line,
        "progress": _progress(todos.data if todos else {}),
        "files": _files(facts, group),
        "place": facts.place,
        "created_at": utc(facts.created_at),
        "updated_at": utc(facts.updated_at),
        "resolved_at": utc(facts.resolved_at),
    }


def _state(facts: ThreadFacts, latest: dict[str, Any], now: datetime) -> tuple[str, str | None, str | None]:
    """The thread's group, reason and status line: the first rule that matches wins.

    ``WorkstreamStore.thread_counts`` counts the groups by these rules in SQL.
    """
    if facts.resolved_at is not None:
        return "resolved", None, _quiet_line(latest)
    waiting = _waiting(facts, latest)
    if waiting is not None:
        return "waiting", *waiting
    if facts.status == "active":
        return "working", *_working(latest)
    if now - aware(facts.updated_at) > QUIET_RESOLVES_AFTER:
        return "resolved", None, _quiet_line(latest)
    return "idle", None, _quiet_line(latest)


def question_of(facts: ThreadFacts) -> Any | None:
    """The question the thread waits on the user to answer, if any: its newest
    pending ``input_required`` item, else one that expired with no message
    after it.  ``ask_user_question`` gives up after 30 minutes, and the answer
    typed into the thread resumes it."""
    asked = [i for i in facts.items if i.kind == "input_required"]
    pending = [i for i in asked if i.status == "pending"]
    if pending:
        return max(pending, key=lambda i: i.source_event_id)
    replied = _messaged(_latest(facts))
    unanswered = [i for i in asked if i.status == "expired" and i.source_event_id > replied]
    return max(unanswered, key=lambda i: i.source_event_id, default=None)


def _waiting(facts: ThreadFacts, latest: dict[str, Any]) -> tuple[str, str | None] | None:
    """Why the thread waits on the user, and what for; None when it does not.

    The newest pending item first, then a failure, then a question that
    expired unanswered.
    """
    pending = [i for i in facts.items if i.status == "pending"]
    if pending:
        newest = max(pending, key=lambda i: i.source_event_id)
        return _reason(newest), newest.title
    if facts.status == "failed":
        failure = latest.get(EventType.SESSION_FAIL.value)
        data = failure.data if failure else {}
        return "failed", _line(data.get("error_title") or data.get("error") or data.get("reason"))
    asked = question_of(facts)
    if asked is not None:
        return "question", asked.title
    return None


def _reason(item: Any) -> str:
    """Why *item* makes its thread wait: a landing's files, else by its kind."""
    if item.kind == "action_required" and (item.payload or {}).get("action_type") == "files":
        return "files"
    return WAITING_KINDS[item.kind]


def _working(latest: dict[str, Any]) -> tuple[str | None, str | None]:
    """A working thread's reason and status line."""
    # The wait for the computer holds until it resumes, or until a new worker
    # wakes the thread, which announces a wait still live again.
    wait = max(
        (latest[t] for t in (EventType.DEVICE_WAITING.value, EventType.DEVICE_RESUMED.value,
                             EventType.HARNESS_WAKE.value) if t in latest),
        key=lambda e: e.id, default=None,
    )
    if wait is not None and wait.type == EventType.DEVICE_WAITING.value:
        return "computer", f"Waiting for {wait.data.get('device_name') or 'your computer'}"
    # An iteration summary from before the latest message is the last turn's.
    summary = latest.get(EventType.ITERATION_SUMMARY.value)
    if summary is not None and summary.id > _messaged(latest):
        return None, _line(summary.data.get("summary"))
    return None, None


def _quiet_line(latest: dict[str, Any]) -> str | None:
    """The last turn's recap; the first line of its answer when it had none.

    A turn that ended early has no summary, so its answer is newer than the
    last recap, which was another turn's.
    """
    summary = latest.get(EventType.TURN_SUMMARY.value)
    answer = latest.get(EventType.LLM_RESPONSE.value)
    if summary is not None and summary.id > _id(answer):
        recap = _line(summary.data.get("recap"))
        if recap:
            return recap
    return _line(((answer.data if answer else {}).get("message") or {}).get("content"))


def _progress(data: dict[str, Any]) -> dict[str, int] | None:
    """The latest todo list as done and total; a cancelled item counts in neither."""
    todos = [t for t in data.get("todos") or [] if isinstance(t, dict) and t.get("status") != "cancelled"]
    if not todos:
        return None
    return {"done": sum(t.get("status") == "completed" for t in todos), "total": len(todos)}


def _files(facts: ThreadFacts, group: str) -> list[dict[str, Any]]:
    """The files the thread's landings changed, with their marks, then the
    artifacts its turn summaries named; at most the shell's limit.

    A thread with no landing works on the real files, as one does in a
    project over the file cap: its files are its turn summaries' too, with
    no mark.
    """
    kinds = ("artifact",) if facts.landings else ("file", "artifact")
    return [*_landed(facts, group), *_summarized(facts, kinds)][: SHELL_LIMITS["files"]]


def _landed(facts: ThreadFacts, group: str) -> list[dict[str, Any]]:
    """Each file the thread's landings changed, once, as its newest landing
    of it left it: those left out first, then those that landed, each
    newest first.

    ``landed``; or ``not_merged`` when the landing left it out, ``redoing``
    while the thread's next turn is to redo it.  A resolved thread takes
    no next turn, so none of its files is being redone.  A file a landing
    deleted is no file to open: it is left out.  So is one whose path the
    shell would refuse as a label.
    """
    redoing = facts.redoing if group != "resolved" else frozenset()
    left_out: list[dict[str, Any]] = []
    landed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for landing in sorted(facts.landings, key=lambda row: row["id"], reverse=True):
        for f in landing["files"]:
            path = f["path"]
            if path in seen:
                continue
            seen.add(path)
            if (f["merged"] and f["after"] is None) or units(path) > SHELL_LIMITS["label"]:
                continue
            mark = "landed" if f["merged"] else ("redoing" if path in redoing else "not_merged")
            (landed if f["merged"] else left_out).append(
                {"kind": "file", "label": path, "ref": path, "thread_id": str(facts.id), "landing": mark}
            )
    return [*left_out, *landed]


def _summarized(facts: ThreadFacts, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    """The files of *kinds* every turn summary named, newest first, each
    once, with no mark.  An entry the shell would refuse (no ref, or a label
    or ref too long) is left out: the shell refuses every row over one such
    entry."""
    files: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    summaries = [e for e in facts.events if e.type == EventType.TURN_SUMMARY.value]
    for summary in sorted(summaries, key=lambda e: e.id, reverse=True):
        for artifact in summary.data.get("artifacts") or []:
            if not isinstance(artifact, dict) or artifact.get("kind") not in kinds:
                continue
            kind, ref, label = artifact["kind"], artifact.get("ref"), artifact.get("label")
            if not isinstance(ref, str) or not ref or (kind, ref) in seen:
                continue
            label = label if isinstance(label, str) and label else ref
            if units(ref) > SHELL_LIMITS["ref"] or units(label) > SHELL_LIMITS["label"]:
                continue
            seen.add((kind, ref))
            files.append({"kind": kind, "label": label, "ref": ref, "thread_id": str(facts.id), "landing": None})
            if len(files) == SHELL_LIMITS["files"]:
                return files
    return files


def place_of(execution: Any) -> dict[str, Any]:
    """Where a thread works, as the shell's ``ThreadPlace``, from its
    ``config.execution``: the cloud, or the user's computer by the name
    stamped when the thread was made.  Whether that computer is online now
    is the reader's to say (``online`` starts false)."""
    if not isinstance(execution, dict) or execution.get("kind") != "device":
        return {"kind": "cloud"}
    return {
        "kind": "device", "device_id": execution["device_id"], "device_name": execution["device_name"], "online": False,
    }


def units(text: str) -> int:
    """*text*'s length in UTF-16 units, as the shell counts it."""
    return len(text.encode("utf-16-le")) // 2


def _line(text: Any) -> str | None:
    """The first non-blank line of *text*, cut to the status line's length."""
    line = next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")
    if len(line) > _STATUS_LINE_MAX:
        line = line[: _STATUS_LINE_MAX - 1] + "…"
    return line or None


def _latest(facts: ThreadFacts) -> dict[str, Any]:
    """The newest of the facts' events of each type."""
    latest: dict[str, Any] = {}
    for event in sorted(facts.events, key=lambda e: e.id):
        latest[event.type] = event
    return latest


def _id(event: Any) -> int:
    return event.id if event is not None else 0


def _messaged(latest: dict[str, Any]) -> int:
    """The id of the thread's newest message, the user's or its coordinator's follow-up; 0 with none."""
    return max(_id(latest.get(t.value)) for t in MESSAGE_TYPES)


def aware(moment: datetime) -> datetime:
    """*moment* in UTC: sessions' times are naive UTC, and a browser would read them as local time."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def utc(moment: datetime | None) -> str | None:
    """*moment* as the wire carries it: UTC, with a trailing ``Z``."""
    if moment is None:
        return None
    return aware(moment).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
