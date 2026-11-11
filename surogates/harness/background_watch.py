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

While a command is watched, its sandbox outlives the turn that started it
(``holds``): a turn's end leaves it, and the watch lets it go once the last
command it watches has ended and the session is at rest.  A sandbox that went
anyway (a stop, a failed turn, a new pod) took its commands with it: each is
reported stopped.

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

#: Failed asks in a row after which a registry is given up on: its commands are reported stopped.
MAX_FAILED_ASKS = 30


@dataclass
class _Watched:
    """A command whose end a session waits to hear."""

    session_id: UUID
    command: str
    started: float = field(default_factory=time.monotonic)


def _status(exit_code: Any) -> str:
    """How a command that exited with *exit_code* ended."""
    if isinstance(exit_code, int) and exit_code < 0:
        return "killed"
    if isinstance(exit_code, int) and exit_code > 0:
        return "failed"
    return "completed"


def completion_payload(proc_id: str, watched: _Watched, entry: dict | None, *, reason: str | None = None) -> dict:
    """The ``agent.notification`` payload of a watched command's end: *entry* is
    the registry's completion; None when the command went without one."""
    if entry is None:
        return {
            "agent_id": proc_id, "kind": "process", "status": "killed",
            "summary": watched.command, "result": "",
            **({"reason": reason} if reason else {}),
        }
    code = entry.get("exit_code")
    return {
        "agent_id": proc_id, "kind": "process", "status": _status(code),
        "summary": watched.command, "exit_code": code,
        "result": str(entry.get("output") or "")[-MAX_RESULT_CHARS:],
    }


class BackgroundWatch:
    """Polls the registries of the background commands this worker started; see the module."""

    def __init__(self, *, poll_seconds: float = POLL_SECONDS) -> None:
        self._poll_seconds = poll_seconds
        self._owners: dict[str, dict[str, _Watched]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
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
        """Whether the sandbox keyed *owner* runs a command the watch waits on: its turn's end leaves it up."""
        return bool(self._owners.get(owner))

    def watch(self, *, owner: str, proc_id: str, session_id: UUID, command: str) -> None:
        """Wait for the end of process *proc_id*, which session *session_id* started in the registry *owner* keys:
        a sandbox's key, or ``HERE`` for this process's."""
        if not self.configured:
            return
        self._owners.setdefault(owner, {})[proc_id] = _Watched(session_id=session_id, command=command)
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

    async def _poll(self, owner: str) -> None:
        failed = 0
        try:
            while self._owners.get(owner):
                await asyncio.sleep(self._poll_seconds)
                try:
                    ended = await self._drain(owner)
                    failed = 0
                except Exception:
                    failed += 1
                    logger.warning("Could not ask %s for its background commands", owner or "this worker", exc_info=True)
                    if failed < MAX_FAILED_ASKS:
                        continue
                    # Its sandbox does not answer: its commands are as good as gone.
                    ended = [(proc_id, None) for proc_id in self._owners.get(owner, {})]
                if not ended:
                    continue
                watched = self._owners.get(owner, {})
                told = [(watched.pop(proc_id), proc_id, entry) for proc_id, entry in ended if proc_id in watched]
                if not watched:
                    self._owners.pop(owner, None)
                    # Let the sandbox go before the session is woken: a turn
                    # the notification gives it may not lose its pod to this.
                    await self._let_go(owner)
                for item, proc_id, entry in told:
                    await self._tell(item, proc_id, entry)
        finally:
            if self._tasks.get(owner) is asyncio.current_task():
                self._tasks.pop(owner, None)

    async def _drain(self, owner: str) -> list[tuple[str, dict | None]]:
        """The watched commands of *owner* that ended since the last ask, each with its completion
        (None for one that went without, its sandbox gone)."""
        watched = self._owners.get(owner, {})
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
                if entry.get("session_id") in watched:
                    ended.append((entry["session_id"], entry))
            return ended
        try:
            raw = await self._sandbox_pool.execute(owner, DRAIN, "{}")
        except ValueError:
            # No sandbox is mapped to it now: it went, and its commands with it.
            return [(proc_id, None) for proc_id in watched]
        answer = json.loads(raw)
        if not isinstance(answer, dict) or "known" not in answer:
            # A sandbox image older than the watch keeps no command past its call.
            logger.warning("The sandbox of %s cannot report its background commands: %s", owner, str(raw)[:200])
            return [(proc_id, None) for proc_id in watched]
        ended = [(e["session_id"], e) for e in answer.get("events") or [] if isinstance(e, dict) and e.get("session_id") in watched]
        done = {proc_id for proc_id, _ in ended}
        known = set(answer.get("known") or ())
        # A command the registry does not know ran in a pod that is gone: this one is newer.
        ended += [(proc_id, None) for proc_id in watched if proc_id not in known and proc_id not in done]
        return ended

    async def _tell(self, item: _Watched, proc_id: str, entry: dict | None) -> None:
        from surogates.harness.worker_notify import notify_parent_of_task_event

        await notify_parent_of_task_event(
            session_store=self._store,
            parent_session_id=item.session_id,
            event_type=EventType.AGENT_NOTIFICATION,
            payload=completion_payload(proc_id, item, entry, reason=None if entry else "its sandbox ended"),
            redis=self._redis,
        )

    async def _let_go(self, owner: str) -> None:
        """Take down the sandbox *owner* keys, kept for the commands it ran, unless a turn uses it now."""
        if owner == HERE or self._sandbox_pool is None:
            return
        try:
            session = await self._store.get_session(UUID(owner))
        except Exception:
            logger.debug("Could not read session %s to let its sandbox go", owner, exc_info=True)
            return
        if session.status == "active":
            # The turn under way takes it down at its end, as any turn's.
            return
        try:
            sandbox_id = await self._sandbox_pool.release_for_session(owner)
            if sandbox_id is not None:
                await self._sandbox_pool.destroy_released(sandbox_id, owner)
        except Exception:
            logger.debug("Could not let the sandbox of %s go", owner, exc_info=True)


#: This worker's watch: configured at worker start (``orchestrator.worker``).
background_watch = BackgroundWatch()
