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

One watch per worker process; the watches of all workers share their state
in Redis (``_Shared``).  The watch that started a command reads it to its end:
the executor's token of the sandbox it runs in is that worker's alone.  A later
turn of the session on another worker sees it all the same: ``list_agents``
lists it, ``stop_agent`` asks its watch to kill it, and the monitors' count and
rate are the session's.  A worker that dies stops renewing its heartbeat
(``alive``, which also tells the Docker backend whose sandboxes a sweep of a
session's containers must spare: ``DockerSandbox.claim_as``); the next sweep
of another worker's watch tells each session it watched that its command can
no longer be watched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

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

#: The Redis keys the watches of all workers share (``_Shared``).
SHARED_PREFIX = "surogates:bgwatch"
#: Seconds a watch's heartbeat lives: it renews it at every poll and sweep.
ALIVE_SECONDS = 60
#: Seconds between two sweeps for the commands of a watch whose worker is gone.
SWEEP_SECONDS = 15.0
#: Why a session hears that a command another worker watched cannot be watched any more.
WATCH_LOST = "the worker that watched it stopped; it may have stopped with it, or may still be running"

#: Adds a send to a session's minute of monitor notes if it has room: atomic, for the watches of all workers.
_RATE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - 60)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[3])
redis.call('EXPIRE', KEYS[1], 120)
return 1
"""

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


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class _Shared:
    """The state the watches of all workers share, in Redis; see the module.  With no Redis, each answer is this
    watch's alone (one worker).

    Keys, under ``SHARED_PREFIX``: ``cmd:<session>:<proc>`` (a watched command: its sandbox key, session, command,
    monitor description, and the watch that reads it), ``session:<id>`` and ``all`` (sets of ``<session>:<proc>``),
    ``alive:<watch>`` (its heartbeat), ``stop:<watch>`` (``<session>:<proc>`` another worker asked it to stop),
    ``rate:<session>`` (its monitor notes of the last minute).  A command is always named with its session: the
    process id comes from the sandbox, and one session's id never reaches another session's command.
    """

    def __init__(self, redis: Any, me: str) -> None:
        self.redis, self.me = redis, me
        #: When this process first found each other watch's heartbeat missing (``presumed_dead``).
        self._missing_since: dict[str, float] = {}

    def key(self, *parts: Any) -> str:
        return ":".join([SHARED_PREFIX, *(str(p) for p in parts)])

    async def add(self, proc_id: str, owner: str, session_id: UUID, command: str, monitor: str | None) -> None:
        if self.redis is None:
            return
        ttl = int(MAX_WATCH_SECONDS + GRACE_SECONDS + 600)
        name = f"{session_id}:{proc_id}"
        entry = json.dumps({
            # The command line, cut as ``list_agents`` shows it: what it may hold stays out of Redis.
            "proc_id": proc_id, "owner": owner, "session_id": str(session_id), "command": command[:200],
            "monitor": monitor, "watch": self.me,
        })
        await self.redis.set(self.key("cmd", name), entry, ex=ttl)
        await self.redis.sadd(self.key("session", session_id), name)
        await self.redis.expire(self.key("session", session_id), ttl)
        await self.redis.sadd(self.key("all"), name)
        await self.alive()

    async def remove(self, proc_id: str, session_id: Any) -> None:
        if self.redis is None:
            return
        name = f"{session_id}:{proc_id}"
        await self.redis.delete(self.key("cmd", name))
        await self.redis.srem(self.key("session", session_id), name)
        await self.redis.srem(self.key("all"), name)

    async def entry(self, name: str) -> dict | None:
        """The command ``<session>:<proc>`` names, if a watch still reads it and it is that session's."""
        if self.redis is None:
            return None
        raw = await self.redis.get(self.key("cmd", name))
        try:
            entry = json.loads(_text(raw)) if raw is not None else None
        except ValueError:
            return None
        if not isinstance(entry, dict) or f"{entry.get('session_id')}:{entry.get('proc_id')}" != name:
            return None
        return entry

    async def of_session(self, session_id: UUID) -> list[dict] | None:
        """The commands session *session_id* started that a watch still reads; None with no Redis."""
        if self.redis is None:
            return None
        found = []
        for name in sorted(_text(p) for p in await self.redis.smembers(self.key("session", session_id))):
            entry = await self.entry(name) if name.startswith(f"{session_id}:") else None
            if entry is None:
                await self.redis.srem(self.key("session", session_id), name)
            else:
                found.append(entry)
        return found

    async def alive(self) -> None:
        if self.redis is not None:
            await self.redis.set(self.key("alive", self.me), "1", px=int(ALIVE_SECONDS * 1000))

    async def gone(self) -> None:
        """This watch stops: the next sweep of another reports what it read."""
        if self.redis is not None:
            await self.redis.delete(self.key("alive", self.me))

    async def presumed_dead(self, watch: str) -> bool:
        """Whether the worker of *watch* is gone: its heartbeat has been missing for ``ALIVE_SECONDS`` as this
        process has looked.  A heartbeat a Redis restart or flush lost is back within a sweep, long before."""
        if watch == self.me:
            return False
        if self.redis is None:
            return True
        now = time.monotonic()
        if await self.redis.exists(self.key("alive", watch)):
            self._missing_since.pop(watch, None)
            return False
        return now - self._missing_since.setdefault(watch, now) >= ALIVE_SECONDS

    async def ask_stop(self, watch: str, session_id: Any, proc_id: str) -> None:
        if self.redis is not None:
            await self.redis.rpush(self.key("stop", watch), f"{session_id}:{proc_id}")
            await self.redis.expire(self.key("stop", watch), 120)

    async def stop_requests(self) -> list[str]:
        """The ``<session>:<proc>`` other workers asked this watch to stop."""
        if self.redis is None:
            return []
        asked = []
        while (name := await self.redis.lpop(self.key("stop", self.me))) is not None:
            asked.append(_text(name))
        return asked

    async def rate_ok(self, session_id: UUID) -> bool:
        """Count a monitor note to *session_id* if its minute has room: ``MAX_NOTES_PER_SESSION_MINUTE``, all
        workers' watches together."""
        now = time.time()
        return bool(await self.redis.eval(
            _RATE_SCRIPT, 1, self.key("rate", session_id), now, MAX_NOTES_PER_SESSION_MINUTE, f"{now}:{uuid4().hex}",
        ))

    async def orphans(self) -> list[dict]:
        """Commands whose watch's worker is gone, each claimed by this watch once: their sessions are told."""
        if self.redis is None:
            return []
        found = []
        for name in [_text(p) for p in await self.redis.smembers(self.key("all"))]:
            entry = await self.entry(name)
            if entry is None:
                await self.redis.srem(self.key("all"), name)
                continue
            if not await self.presumed_dead(str(entry.get("watch"))):
                continue
            if await self.redis.set(self.key("claim", name), self.me, nx=True, ex=600):
                found.append(entry)
        return found


class BackgroundWatch:
    """Polls the registries of the background commands this worker started; see the module."""

    def __init__(self, *, poll_seconds: float = POLL_SECONDS) -> None:
        self._poll_seconds = poll_seconds
        self._owners: dict[str, dict[str, _Watched]] = {}
        self._holdable: dict[str, bool] = {}
        self._releases: dict[str, _Release] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: When each session was last sent its monitors' lines, within the last minute (with no Redis).
        self._session_sent: dict[UUID, list[float]] = {}
        self._store: Any = None
        self._redis: Any = None
        self._sandbox_pool: Any = None
        #: This watch, among the watches of all workers.
        self.id = uuid4().hex
        self._shared = _Shared(None, self.id)
        self._sweeper: asyncio.Task[None] | None = None
        #: Commands another worker asked this watch to stop, until the poll of their sandbox kills them.
        self._stop_asked: set[str] = set()

    def configure(self, *, store: Any, redis: Any, sandbox_pool: Any) -> None:
        """Give the watch what it tells sessions through and polls sandboxes with."""
        self._store, self._redis, self._sandbox_pool = store, redis, sandbox_pool
        self._shared = _Shared(redis, self.id)

    def start_sweeping(self, *, every: float = SWEEP_SECONDS) -> None:
        """Look, every *every* seconds, for commands whose watch's worker is gone, and tell their sessions."""
        if self._redis is not None and (self._sweeper is None or self._sweeper.done()):
            self._sweeper = asyncio.create_task(self._sweep(every), name="background-watch-sweep")

    @property
    def configured(self) -> bool:
        return self._store is not None

    def holds(self, owner: str) -> bool:
        """Whether the sandbox keyed *owner* is kept past a turn's end: it runs a command the watch waits on, or
        its last one just ended and the turn its end gives may want it.  Never a project thread's."""
        return self._holdable.get(owner, True) and (bool(self._owners.get(owner)) or owner in self._releases)

    async def worker_alive(self, watch: str) -> bool:
        """Whether the worker whose watch is *watch* lives: its heartbeat stands, or has been missing for less than
        ``ALIVE_SECONDS``.  Unsure (Redis did not answer), it does: what it owns is spared."""
        try:
            return not await self._shared.presumed_dead(watch)
        except Exception:
            logger.warning("Could not read whether worker %s lives", watch, exc_info=True)
            return True

    async def running_for(self, session_id: UUID) -> list[dict]:
        """The commands session *session_id* started that a watch still waits on: this worker's, and those of all
        workers that Redis knows."""
        found = {
            proc_id: {"id": proc_id, "command": item.command[:200], "status": "running",
                      **({"monitor": item.monitor.description} if item.monitor is not None else {})}
            for watched in self._owners.values() for proc_id, item in watched.items() if item.session_id == session_id
        }
        try:
            shared = await self._shared.of_session(session_id) if self.configured else None
        except Exception:
            logger.warning("Could not read the commands other workers watch for %s", session_id, exc_info=True)
            shared = None
        for e in shared or []:
            found.setdefault(str(e["proc_id"]), {
                "id": str(e["proc_id"]), "command": str(e.get("command") or "")[:200], "status": "running",
                **({"monitor": e["monitor"]} if e.get("monitor") else {}),
            })
        return [found[proc_id] for proc_id in sorted(found)]

    async def monitors_running(self, session_id: UUID) -> int:
        """How many monitors session *session_id* runs, on any worker."""
        return sum(1 for command in await self.running_for(session_id) if command.get("monitor"))

    async def stop_elsewhere(self, proc_id: str, session_id: UUID) -> str | None:
        """Ask the watch of another worker that reads command *proc_id*, one session *session_id* started, to kill
        it: ``stopping``, its end then reaching the session as a notification; ``gone`` when that worker is gone;
        None when no other worker watches such a command."""
        try:
            entry = await self._shared.entry(f"{session_id}:{proc_id}") if self.configured else None
            if entry is None or entry.get("watch") == self.id:
                return None
            if await self._shared.presumed_dead(str(entry.get("watch"))):
                return "gone"
            await self._shared.ask_stop(str(entry.get("watch")), session_id, proc_id)
        except Exception:
            logger.warning("Could not ask another worker to stop %s", proc_id, exc_info=True)
            return None
        return "stopping"

    async def stop_session_elsewhere(self, session_id: UUID) -> None:
        """Ask the watches of other workers to kill every command session *session_id* started there: its user
        stopped it, or cleared it.  This worker's go with the sandbox the stop takes down, as before; another
        worker's sandbox is that worker's to stop.  Their ends reach the session as notifications."""
        for command in await self.running_for(session_id):
            if not self.started_by(command["id"], session_id):
                await self.stop_elsewhere(command["id"], session_id)

    def owner_of(self, proc_id: str) -> str | None:
        """The registry key of watched command *proc_id*: a sandbox's key, ``HERE``, or None for one not watched."""
        return next((owner for owner, watched in self._owners.items() if proc_id in watched), None)

    def started_by(self, proc_id: str, session_id: UUID) -> bool:
        """Whether this watch waits on command *proc_id* for session *session_id*."""
        return any(
            (item := watched.get(proc_id)) is not None and item.session_id == session_id
            for watched in self._owners.values()
        )

    async def watch(
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
        try:
            await self._shared.add(proc_id, owner, session_id, command, monitor[0] if monitor is not None else None)
        except Exception:
            logger.warning("Could not share background command %s with the other workers", proc_id, exc_info=True)

    async def close(self) -> None:
        """Stop polling: the worker is going.  The commands it read are told by the next sweep of another
        worker's watch."""
        tasks = list(self._tasks.values()) + ([self._sweeper] if self._sweeper is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self._shared.gone()
        except Exception:
            logger.debug("Could not end this watch's heartbeat", exc_info=True)
        self._sweeper = None
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
                await self._share_state(owner)
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
                    if not await self._session_has_room(item.session_id, now):
                        continue
                    # As many as one note holds: the rest wait for the next.
                    count = _fitting(monitor.buffered[:MAX_LINES_PER_NOTE]) or 1
                    sending, monitor.buffered = monitor.buffered[:count], monitor.buffered[count:]
                    payloads.append((item, monitor_payload(proc_id, monitor, lines=sending, dropped=monitor.dropped)))
                    monitor.dropped = 0
                    monitor.sent.append(now)
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
                # Once told: a worker that dies before it has told keeps the command in the shared state, for the
                # sweep of another to tell.
                for item, payload in payloads:
                    if payload.get("status") != "output":
                        await self._unshare(str(payload.get("agent_id")), item.session_id)
                if release is not None:
                    release.told = told
        finally:
            if self._tasks.get(owner) is asyncio.current_task():
                self._tasks.pop(owner, None)

    async def _share_state(self, owner: str) -> None:
        """Renew this watch's heartbeat, and kill what another worker asked it to stop in *owner*'s sandbox: their
        ends come with the next drain, as a stop's here does."""
        try:
            await self._shared.alive()
            self._stop_asked.update(await self._shared.stop_requests())
        except Exception:
            logger.warning("Could not share the watch's state with the other workers", exc_info=True)
        here = self._owners.get(owner, {})
        # Each ask names its session: only the command that session started is killed.
        for name in [n for n in self._stop_asked if (item := here.get(n.partition(":")[2])) is not None
                     and n.partition(":")[0] == str(item.session_id)]:
            self._stop_asked.discard(name)
            await self._kill(owner, name.partition(":")[2])
        # Asked of a command that ended meanwhile, or of no command of this watch's: nothing to do.
        watched = {f"{item.session_id}:{p}" for each in self._owners.values() for p, item in each.items()}
        self._stop_asked &= watched

    async def _unshare(self, proc_id: str, session_id: UUID) -> None:
        try:
            await self._shared.remove(proc_id, session_id)
        except Exception:
            logger.warning("Could not tell the other workers that %s ended", proc_id, exc_info=True)

    async def _session_has_room(self, session_id: UUID, now: float) -> bool:
        """Whether session *session_id* may get one more monitor note this minute, counted for all workers'
        watches in Redis, or for this one's alone."""
        if self._redis is not None:
            try:
                return await self._shared.rate_ok(session_id)
            except Exception:
                logger.warning("Could not count the monitor notes of session %s", session_id, exc_info=True)
        sent = self._session_sent.setdefault(session_id, [])
        sent[:] = [t for t in sent if now - t < 60.0]
        if len(sent) >= MAX_NOTES_PER_SESSION_MINUTE:
            return False
        sent.append(now)
        return True

    async def _sweep(self, every: float) -> None:
        """Tell the sessions of commands whose watch's worker is gone that they cannot be watched any more."""
        while True:
            try:
                await self._shared.alive()
                for entry in await self._shared.orphans():
                    session_id = UUID(str(entry["session_id"]))
                    item = _Watched(session_id=session_id, command=str(entry.get("command") or ""))
                    proc_id = str(entry["proc_id"])
                    if entry.get("monitor"):
                        item.monitor = _Monitor(description=str(entry["monitor"]), deadline=0.0, cap=1)
                        payload = monitor_end(proc_id, item, None, status="lost", reason=WATCH_LOST)
                    else:
                        payload = completion_payload(proc_id, item, None, reason=WATCH_LOST, status="lost")
                    if await self._tell(session_id, payload) is not None:
                        await self._shared.remove(proc_id, session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("The sweep for commands of gone workers failed", exc_info=True)
            await asyncio.sleep(every)

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
