"""The watch on background commands a session asked to hear the end of.

``terminal(background=true, notify_on_complete=true)`` starts a command that
outlives the call: the call returns at once with the process's id.  Its
process registry knows when it ends, but tells no one.  The registry lives
where the command runs: in the sandbox's executor daemon
(``executor_server``), or in this process for a session with no sandbox.

The watch asks that registry, every ``POLL_SECONDS``, for the ends of the
commands this worker started, and writes each to the session that started
it as an ``agent.notification`` (``agent_news``), which wakes the session
when it rests.  Only the session recorded here when the call returned is
told: a command's end never reaches a session that did not start it.

The sandbox outlives the turn that started such a command (``holds``), unless
it is a project thread's, which is made for one turn.  Once the last command
it ran has ended, it is kept for the turn the notification gives, and let go
when no session of its tree is running and that turn has ended, or after
``GRACE_SECONDS``.  A command is watched for at most ``MAX_WATCH_SECONDS``;
then it is killed and reported so.  A sandbox that went anyway (a stop, a
failed turn, a new pod) took its commands with it: each is reported stopped.

A monitor (``monitor`` tool) is a watched command whose output lines are
news too: each poll's new lines go to its session in one notification, at most
``cap`` notifications a minute, and at most ``MAX_NOTES_PER_SESSION_MINUTE``
for all of a session's monitors (the rest wait, ``MAX_BUFFERED_LINES`` of them;
more are counted and left out), until the command ends, its timeout passes
(it is killed), it is stopped, or its session is stopped (it is killed).

One watch per worker process.  A worker that dies takes its watch along:
the commands' ends are then never told.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from surogates.harness.agent_news import MAX_RESULT_CHARS
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

#: Seconds between two asks of a registry.
POLL_SECONDS = 2.0

#: The internal executor call that drains the daemon's completions (``executor_server``).
DRAIN = "_background"

#: The owner key of the registry in this process (a session with no sandbox).
HERE = ""

#: Failed asks in a row after which a registry is given up on.
MAX_FAILED_ASKS = 30

#: The longest a command is watched; then it is killed.
MAX_WATCH_SECONDS = 3600.0

#: How long a sandbox is kept for the turn its last command's end gives, at most.
GRACE_SECONDS = 600.0

#: Lines a monitor holds back for its next notification; more are left out and counted.
MAX_BUFFERED_LINES = 200
#: The most lines one notification carries.
MAX_LINES_PER_NOTE = 50
#: The longest line the watch takes from a registry; the registry cuts them there too.
MAX_LINE_CHARS = 2000
#: The most notifications all of one session's monitors send in a minute.
MAX_NOTES_PER_SESSION_MINUTE = 30
#: The most monitors one session runs at a time, counted by the registry where they run (``monitor_task``).
MAX_MONITORS_PER_SESSION = 4
#: A session in one of these is not sent its monitors' lines: they are killed.
_STOPPED = ("paused", "archived")

#: An executor too old to keep background commands answers the drain so.
_UNKNOWN_DRAIN = f"Unknown tool: {DRAIN}"


@dataclass
class _Monitor:
    """What a monitor's lines are sent under: its name, its deadline and its rate."""

    description: str
    deadline: float
    cap: int
    buffered: list[str] = field(default_factory=list)
    dropped: int = 0
    sent: list[float] = field(default_factory=list)

    def take(self, lines: list[str], dropped: int) -> None:
        room = MAX_BUFFERED_LINES - len(self.buffered)
        self.buffered += lines[:max(room, 0)]
        self.dropped += dropped + max(len(lines) - max(room, 0), 0)

    def may_send(self, now: float) -> bool:
        self.sent = [t for t in self.sent if now - t < 60.0]
        return len(self.sent) < self.cap


@dataclass
class _Watched:
    """A command whose end a session waits to hear."""

    session_id: UUID
    command: str
    started: float = field(default_factory=time.monotonic)
    monitor: _Monitor | None = None


@dataclass
class _Release:
    """A sandbox whose watched commands have all ended, kept for the turns their ends give."""

    since: float
    #: Each session told, with the id of the notification it was told by.
    told: list[tuple[UUID, int | None]]


def _read_lines(got: Any) -> tuple[list[str], int]:
    """A registry's answer for one monitor, (its lines, how many it left out), read with no trust in its shape:
    the sandbox's daemon answers it, and what runs there may have answered in its place."""
    if not isinstance(got, dict):
        return [], 0
    raw = got.get("lines")
    lines = [str(line)[:MAX_LINE_CHARS] for line in raw] if isinstance(raw, list) else []
    try:
        dropped = max(int(got.get("dropped") or 0), 0)
    except (TypeError, ValueError):
        dropped = 0
    return lines, dropped


class _Unwatchable(Exception):
    """The registry cannot report its commands; the message says why."""


def monitor_task(session_id: Any) -> str:
    """The registry task of session *session_id*'s monitors: the registry counts those still running."""
    return f"monitor:{session_id}"


def _exit_code(entry: dict) -> int | None:
    """A completion's exit code, an int or None: the sandbox's daemon answers it, and what runs there may
    have answered in its place."""
    code = entry.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _status(exit_code: Any) -> str:
    """How a command that exited with *exit_code* ended."""
    if isinstance(exit_code, int) and exit_code < 0:
        return "killed"
    if isinstance(exit_code, int) and exit_code > 0:
        return "failed"
    return "completed"


def completion_payload(
    proc_id: str, watched: _Watched, entry: dict | None, *, reason: str | None = None, status: str = "killed",
) -> dict:
    """The ``agent.notification`` payload of a watched command's end: *entry* is
    the registry's completion; None when the command went without one, *status*
    and *reason* then saying how."""
    if entry is None:
        return {
            "agent_id": proc_id, "kind": "process", "status": status,
            "summary": watched.command, "result": "",
            **({"reason": reason} if reason else {}),
        }
    code = _exit_code(entry)
    return {
        "agent_id": proc_id, "kind": "process", "status": _status(code),
        "summary": watched.command, "exit_code": code,
        "result": str(entry.get("output") or "")[-MAX_RESULT_CHARS:],
    }


def monitor_payload(proc_id: str, monitor: _Monitor, *, status: str = "output", lines: list[str] | None = None,
                    dropped: int = 0, exit_code: Any = None, reason: str | None = None) -> dict:
    """The ``agent.notification`` payload of a monitor's new lines (``status`` ``output``) or of its end."""
    kept = (lines or [])[:_fitting(lines or [])]
    # Lines past the note's size are counted with those left out: its header never names more than it shows.
    dropped += len(lines or []) - len(kept)
    payload: dict[str, Any] = {
        "agent_id": proc_id, "kind": "monitor", "status": status, "summary": monitor.description,
        "result": "\n".join(kept), "lines": len(kept),
    }
    if dropped:
        payload["dropped"] = dropped
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if reason:
        payload["reason"] = reason
    return payload


def _fitting(lines: list[str]) -> int:
    """How many of *lines*, from the first, one note's ``MAX_RESULT_CHARS`` holds."""
    size = -1
    for count, line in enumerate(lines):
        size += len(line) + 1
        if size > MAX_RESULT_CHARS:
            return count
    return len(lines)


def monitor_end(proc_id: str, item: _Watched, entry: dict | None, *, status: str, reason: str | None) -> dict:
    """A monitor's last notification: how it ended, with the lines it still held."""
    monitor = item.monitor
    dropped = monitor.dropped + max(len(monitor.buffered) - MAX_LINES_PER_NOTE, 0)
    lines, monitor.buffered = monitor.buffered[:MAX_LINES_PER_NOTE], []
    if entry is not None:
        code = _exit_code(entry)
        return monitor_payload(proc_id, monitor, status=_status(code), lines=lines, dropped=dropped, exit_code=code)
    return monitor_payload(proc_id, monitor, status=status, lines=lines, dropped=dropped, reason=reason)


class BackgroundWatch:
    """Polls the registries of the background commands this worker started; see the module."""

    def __init__(self, *, poll_seconds: float = POLL_SECONDS) -> None:
        self._poll_seconds = poll_seconds
        self._owners: dict[str, dict[str, _Watched]] = {}
        self._holdable: dict[str, bool] = {}
        self._releases: dict[str, _Release] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: When each session was last sent its monitors' lines, within the last minute.
        self._session_sent: dict[UUID, list[float]] = {}
        self._store: Any = None
        self._redis: Any = None
        self._sandbox_pool: Any = None

    def configure(self, *, store: Any, redis: Any, sandbox_pool: Any) -> None:
        """Give the watch what it tells sessions through and polls sandboxes with."""
        self._store, self._redis, self._sandbox_pool = store, redis, sandbox_pool

    @property
    def configured(self) -> bool:
        return self._store is not None

    def holds(self, owner: str) -> bool:
        """Whether the sandbox keyed *owner* is kept past a turn's end: it runs a command the watch waits on, or
        its last one just ended and the turn its end gives may want it.  Never a project thread's."""
        return self._holdable.get(owner, True) and (bool(self._owners.get(owner)) or owner in self._releases)

    def running_for(self, session_id: UUID) -> list[dict]:
        """The commands session *session_id* started that this watch still waits on."""
        return [
            {"id": proc_id, "command": item.command[:200], "status": "running"}
            for watched in self._owners.values() for proc_id, item in watched.items() if item.session_id == session_id
        ]

    def owner_of(self, proc_id: str) -> str | None:
        """The registry key of watched command *proc_id*: a sandbox's key, ``HERE``, or None for one not watched."""
        return next((owner for owner, watched in self._owners.items() if proc_id in watched), None)

    def started_by(self, proc_id: str, session_id: UUID) -> bool:
        """Whether this watch waits on command *proc_id* for session *session_id*."""
        return any(
            (item := watched.get(proc_id)) is not None and item.session_id == session_id
            for watched in self._owners.values()
        )

    def watch(
        self, *, owner: str, proc_id: str, session_id: UUID, command: str,
        monitor: tuple[str, float, int] | None = None,
    ) -> None:
        """Wait for the end of process *proc_id*, which session *session_id* started in the registry *owner* keys:
        a sandbox's key, or ``HERE`` for this process's.  *monitor*, (description, timeout in seconds, the most
        notifications a minute), makes it a monitor whose lines are sent too."""
        if not self.configured:
            return
        watched = _Watched(session_id=session_id, command=command)
        if monitor is not None:
            description, timeout, cap = monitor
            watched.monitor = _Monitor(description=description, deadline=time.monotonic() + timeout, cap=max(1, cap))
        self._owners.setdefault(owner, {})[proc_id] = watched
        self._releases.pop(owner, None)
        if owner != HERE and owner not in self._holdable:
            holds_copy = getattr(self._sandbox_pool, "holds_copy", None)
            # A thread's pod is made for one turn: its turn's end takes it down as always.
            self._holdable[owner] = not (holds_copy is not None and holds_copy(owner))
        task = self._tasks.get(owner)
        if task is None or task.done():
            self._tasks[owner] = asyncio.create_task(self._poll(owner), name=f"background-watch-{owner or 'here'}")

    async def close(self) -> None:
        """Stop polling: the worker is going."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._owners.clear()
        self._releases.clear()
        self._holdable.clear()
        self._session_sent.clear()

    async def _poll(self, owner: str) -> None:
        failed = 0
        try:
            while self._owners.get(owner) or owner in self._releases:
                await asyncio.sleep(self._poll_seconds)
                if not self._owners.get(owner):
                    await self._maybe_let_go(owner)
                    continue
                gone_why, gone_status = "its sandbox ended", "killed"
                lines: dict[str, dict] = {}
                try:
                    ended, lines = await self._drain(owner)
                    failed = 0
                except _Unwatchable as exc:
                    gone_why, gone_status = str(exc), "lost"
                    ended = [(proc_id, None) for proc_id in self._owners.get(owner, {})]
                except Exception:
                    failed += 1
                    logger.warning("Could not ask %s for its background commands", owner or "this worker", exc_info=True)
                    if failed < MAX_FAILED_ASKS:
                        continue
                    gone_why, gone_status = "its sandbox stopped answering; it may still be running", "lost"
                    ended = [(proc_id, None) for proc_id in self._owners.get(owner, {})]
                payloads: list[tuple[_Watched, dict]] = []
                watched = self._owners.get(owner, {})
                for proc_id, got in lines.items():
                    item = watched.get(proc_id)
                    if item is not None and item.monitor is not None:
                        item.monitor.take(*_read_lines(got))
                for proc_id, entry in ended:
                    item = watched.pop(proc_id, None)
                    if item is not None and item.monitor is not None:
                        payloads.append((item, monitor_end(
                            proc_id, item, entry, status=gone_status, reason=None if entry is not None else gone_why,
                        )))
                    elif item is not None:
                        payloads.append((item, completion_payload(
                            proc_id, item, entry, reason=None if entry is not None else gone_why, status=gone_status,
                        )))
                for proc_id, item, why in await self._expire(owner):
                    watched.pop(proc_id, None)
                    payloads.append((item, (
                        monitor_end(proc_id, item, None, status="killed", reason=why) if item.monitor is not None
                        else completion_payload(proc_id, item, None, reason=why)
                    )))
                for proc_id, item in await self._of_stopped_sessions(owner):
                    watched.pop(proc_id, None)
                    payloads.append((item, monitor_end(
                        proc_id, item, None, status="killed", reason="its session was stopped",
                    )))
                now = time.monotonic()
                for proc_id, item in watched.items():
                    monitor = item.monitor
                    if monitor is None or not (monitor.buffered or monitor.dropped) or not monitor.may_send(now):
                        continue
                    sent = self._session_sent.setdefault(item.session_id, [])
                    sent[:] = [t for t in sent if now - t < 60.0]
                    if len(sent) >= MAX_NOTES_PER_SESSION_MINUTE:
                        continue
                    # As many as one note holds: the rest wait for the next.
                    count = _fitting(monitor.buffered[:MAX_LINES_PER_NOTE]) or 1
                    sending, monitor.buffered = monitor.buffered[:count], monitor.buffered[count:]
                    payloads.append((item, monitor_payload(proc_id, monitor, lines=sending, dropped=monitor.dropped)))
                    monitor.dropped = 0
                    monitor.sent.append(now)
                    sent.append(now)
                live = {item.session_id for each in self._owners.values() for item in each.values()}
                for session_id in [sid for sid in self._session_sent if sid not in live]:
                    self._session_sent.pop(session_id, None)
                if not payloads:
                    continue
                release = None
                if not watched:
                    self._owners.pop(owner, None)
                    if owner != HERE:
                        # Before anyone is told: a turn's end meanwhile must keep the sandbox for the woken turn.
                        release = self._releases[owner] = _Release(since=time.monotonic(), told=[])
                told = [(item.session_id, await self._tell(item.session_id, payload)) for item, payload in payloads]
                if release is not None:
                    release.told = told
        finally:
            if self._tasks.get(owner) is asyncio.current_task():
                self._tasks.pop(owner, None)

    async def _drain(self, owner: str) -> tuple[list[tuple[str, dict | None]], dict[str, dict]]:
        """The watched commands of *owner* that ended since the last ask, each with its completion
        (None for one that went without, its sandbox gone); and the monitors' new lines."""
        # The commands watched when the ask went out: one watched meanwhile was
        # started after the answer was made, and is not judged by it.
        asked = set(self._owners.get(owner, {}))
        if owner == HERE:
            from surogates.tools.utils.process_registry import process_registry

            ended: list[tuple[str, dict | None]] = []
            while True:
                try:
                    entry = process_registry.completion_queue.get_nowait()
                except queue.Empty:
                    break
                # The watch is the queue's only reader: a command no session
                # waits on was started with no watch to hear it, and is dropped.
                if entry.get("session_id") in self._owners.get(owner, {}):
                    ended.append((entry["session_id"], entry))
            return ended, process_registry.drain_lines(set(self._owners.get(owner, {})))
        try:
            # Only this watch's commands: another worker's watch on the same sandbox drains its own.
            raw = await self._sandbox_pool.execute(owner, DRAIN, json.dumps({"ids": sorted(asked)}))
        except ValueError:
            # No sandbox is mapped to it now: it went, and its commands with it.
            return [(proc_id, None) for proc_id in asked], {}
        answer = json.loads(raw)
        if not isinstance(answer, dict) or "known" not in answer:
            if isinstance(answer, dict) and _UNKNOWN_DRAIN in str(answer.get("error") or ""):
                raise _Unwatchable("this sandbox cannot report the ends of background commands; it may still be running")
            raise RuntimeError(f"the drain of {owner} answered {str(raw)[:200]}")
        current = self._owners.get(owner, {})
        ended = [
            (e["session_id"], e) for e in answer.get("events") or []
            if isinstance(e, dict) and e.get("session_id") in current
        ]
        done = {proc_id for proc_id, _ in ended}
        known = set(answer.get("known") or ())
        # A command the registry does not know ran in a pod that is gone.
        ended += [(proc_id, None) for proc_id in asked if proc_id not in known and proc_id not in done and proc_id in current]
        lines = answer.get("lines")
        return ended, lines if isinstance(lines, dict) else {}

    async def _expire(self, owner: str) -> list[tuple[str, _Watched, str]]:
        """The watched commands of *owner* past ``MAX_WATCH_SECONDS``, killed, each with why."""
        now = time.monotonic()
        expired = [
            (proc_id, item) for proc_id, item in self._owners.get(owner, {}).items()
            if now - item.started >= MAX_WATCH_SECONDS or (item.monitor is not None and now >= item.monitor.deadline)
        ]
        why = f"it ran longer than {int(MAX_WATCH_SECONDS // 60)} minutes, the most a watched command may run"
        return [
            (
                proc_id, item,
                ("its timeout passed" if item.monitor is not None and now >= item.monitor.deadline else why)
                + ("" if await self._kill(owner, proc_id) else "; it could not be killed and may still be running"),
            )
            for proc_id, item in expired
        ]

    async def _kill(self, owner: str, proc_id: str) -> bool:
        """Kill watched command *proc_id* of *owner*; whether it is known to be gone."""
        try:
            if owner == HERE:
                from surogates.tools.utils.process_registry import process_registry

                answer: Any = process_registry.kill_process(proc_id)
            else:
                answer = json.loads(await self._sandbox_pool.execute(
                    owner, "process", json.dumps({"action": "kill", "session_id": proc_id}),
                ))
        except Exception:
            logger.warning("Could not kill background command %s of %s", proc_id, owner, exc_info=True)
            return False
        return isinstance(answer, dict) and answer.get("status") in ("killed", "already_exited", "not_found")

    async def _of_stopped_sessions(self, owner: str) -> list[tuple[str, _Watched]]:
        """The monitors of *owner* with lines to send whose session was stopped meanwhile, killed: a stopped
        session is not sent lines it would read only once resumed."""
        waiting = {
            item.session_id for item in self._owners.get(owner, {}).values()
            if item.monitor is not None and (item.monitor.buffered or item.monitor.dropped)
        }
        stopped: set[UUID] = set()
        from surogates.session.store import SessionNotFoundError

        for session_id in waiting:
            try:
                if (await self._store.get_session(session_id)).status in _STOPPED:
                    stopped.add(session_id)
            except SessionNotFoundError:
                stopped.add(session_id)  # deleted: no one is left to read its lines
            except Exception:
                logger.debug("Could not read the status of session %s", session_id, exc_info=True)
        found = [
            (proc_id, item) for proc_id, item in self._owners.get(owner, {}).items()
            if item.monitor is not None and item.session_id in stopped
        ]
        for proc_id, _ in found:
            await self._kill(owner, proc_id)
        return found

    async def _tell(self, session_id: UUID, payload: dict) -> int | None:
        """Write *payload* to session *session_id* as a notification and queue the session: the event's id."""
        from surogates.channels.constants import session_queue_priority
        from surogates.config import enqueue_session

        try:
            event_id = await self._store.emit_event(session_id, EventType.AGENT_NOTIFICATION, payload)
        except Exception:
            logger.exception("Could not tell session %s that background command %s ended", session_id, payload.get("agent_id"))
            return None
        if self._redis is not None:
            try:
                session = await self._store.get_session(session_id)
                await enqueue_session(
                    self._redis, org_id=str(session.org_id), agent_id=session.agent_id, session_id=session_id,
                    priority=session_queue_priority(session),
                )
            except Exception:
                logger.exception("Told session %s, but could not wake it", session_id)
        return event_id if isinstance(event_id, int) else None

    async def _maybe_let_go(self, owner: str) -> None:
        """Let the sandbox *owner* keys go once its tree rests and the turns its commands' ends gave have ended,
        or once ``GRACE_SECONDS`` have passed.  A turn's end that takes it down first leaves nothing to do."""
        release = self._releases.get(owner)
        sandbox_id = self._sandbox_pool.sandbox_of(owner) if release is not None and self._sandbox_pool is not None else None
        if sandbox_id is None:
            self._releases.pop(owner, None)
            self._holdable.pop(owner, None)
            return
        # The grace is a cap: past it the sandbox goes, whatever of its tree still runs.
        if time.monotonic() - release.since < GRACE_SECONDS:
            try:
                for sid in await self._store.session_tree_ids(UUID(owner)):
                    if (await self._store.get_session(sid)).status in ("active", "processing"):
                        return
                for session_id, told_at in release.told:
                    if told_at is None:
                        continue
                    if not any([
                        await self._store.has_event(session_id, kind, after=told_at)
                        for kind in (EventType.SESSION_COMPLETE, EventType.SESSION_FAIL, EventType.SESSION_PAUSE)
                    ]):
                        return
            except Exception:
                logger.debug("Could not tell whether the tree of %s rests", owner, exc_info=True)
                return
        if self._releases.get(owner) is not release or self._owners.get(owner):
            # A command was watched there meanwhile: the sandbox is kept for it.
            return
        self._releases.pop(owner, None)
        self._holdable.pop(owner, None)
        released = await self._sandbox_pool.release_for_session(owner, only=sandbox_id)
        if released is not None:
            try:
                await self._sandbox_pool.destroy_released(released, owner)
            except Exception:
                logger.debug("Could not take down the sandbox of %s", owner, exc_info=True)


#: This worker's watch: configured at worker start (``orchestrator.worker``).
background_watch = BackgroundWatch()
