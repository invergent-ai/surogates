"""The worker capacity one turn holds, given back while all of the turn waits.

The dispatcher gives each turn two slots: one of the worker's
``max_concurrent`` semaphore slots and one of its tenant's turn-gate slots.

Work that needs the worker runs as an activity: the turn's own task, and each
tool call.  The turn's own task steps out (``joining``) while it waits for its
tool calls.  When every activity still counted is waiting on something
outside the worker -- the user's computer, a person answering, a child
session -- the turn gives both slots back.  Before any activity continues, it
takes them again in the dispatcher's order: the tenant slot, then the
semaphore.

There is one record per turn.  Every task of the turn shares it through
``current_turn`` (a copied context still holds the same record), so parallel
waits count together; nested waits in one task count once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

# The longest one turn-gate call may take: the Redis client sets no socket timeout.
GATE_CALL_TIMEOUT_S = 5.0

# Whether this task is already inside a wait of its turn.
_in_wait: ContextVar[bool] = ContextVar("surogates_turn_in_wait", default=False)


class TurnSlots:
    def __init__(
        self,
        *,
        semaphore: asyncio.Semaphore,
        gate: Any | None,
        org_id: str,
        agent_id: str,
        gate_held: bool,
        gate_holder: str = "",
        session_id: str = "",
        task: asyncio.Task[Any] | None = None,
    ) -> None:
        self._semaphore = semaphore
        self._gate = gate
        self._org_id = org_id
        self._agent_id = agent_id
        self._session_id = session_id
        # The turn's holder in the tenant gate: its slot is added and removed under it.
        self._gate_holder = gate_holder
        # A turn that never had a tenant slot never takes one.
        self._uses_gate = gate is not None and gate_held
        self._gate_held = self._uses_gate
        self._semaphore_held = True
        self._active = 0
        self._waiting = 0
        self._resumable = 0
        self._ended = False
        # Serialises giving back and taking back; the counters change outside
        # it, so a cancellation cannot leave them wrong.
        self._lock = asyncio.Lock()
        # The task the turn runs in: detaching cancels it.
        self._task = task
        self.detached = False
        # Set when the turn is interrupted or ended: a take-back waiting for
        # a semaphore slot stops waiting.
        self._stop_waiting = asyncio.Event()

    @contextlib.asynccontextmanager
    async def activity(self) -> AsyncIterator[None]:
        """Run work that needs the worker, taking the slots back first if the turn gave them up."""
        async with self._lock:
            await self._take_back()
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            async with self._lock:
                await self._give_back_if_all_waiting()

    @contextlib.asynccontextmanager
    async def joining(self) -> AsyncIterator[None]:
        """The turn's own task waits for its tool calls: it stops counting until they return."""
        self._active -= 1
        cancelled = False
        try:
            async with self._lock:
                await self._give_back_if_all_waiting()
            yield
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._active += 1
            if not cancelled:
                async with self._lock:
                    await self._take_back()

    @contextlib.asynccontextmanager
    async def waiting(self, *, resumable: bool = False) -> AsyncIterator[None]:
        """Wait on something outside the worker; once all of the turn waits, the slots go back.

        A resumable wait is one another worker can take over from the
        journal: an operation on the user's computer.  A cancelled wait takes
        nothing back: whoever carries on (the loop, when it stops joining)
        takes the slots back before it works.
        """
        if _in_wait.get():
            yield
            return
        token = _in_wait.set(True)
        self._waiting += 1
        if resumable:
            self._resumable += 1
        cancelled = False
        try:
            async with self._lock:
                await self._give_back_if_all_waiting()
            yield
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._waiting -= 1
            if resumable:
                self._resumable -= 1
            _in_wait.reset(token)
            if not cancelled:
                async with self._lock:
                    if self._waiting < self._active:
                        await self._take_back()

    async def release_owned(self) -> None:
        """End the turn: give back what it still holds, and never take anything again.

        It does not wait behind a take-back blocked on a full worker: that one
        stops waiting.
        """
        self._ended = True
        self._stop_waiting.set()
        async with self._lock:
            await self._give_back()
        if self._gate_held:
            # Nothing gives it back later, and the gate has no TTL.
            logger.error(
                "tenant slot of org=%s agent=%s leaked by session %s (the turn gate has no TTL)",
                self._org_id, self._agent_id, self._session_id,
            )

    @property
    def all_waiting(self) -> bool:
        """Whether every activity still counted is waiting on something outside the worker."""
        return self._active > 0 and self._waiting >= self._active

    @property
    def waiting_resumably(self) -> bool:
        """Whether every activity still counted waits on work another worker can resume from the journal."""
        return self._active > 0 and self._resumable >= self._active

    def detach(self) -> None:
        """Stop the turn so another worker resumes it.

        Its task is cancelled and what it waits on is left as it is: an open
        operation stays in the journal.  It never takes a slot again; what it
        still holds is given back when the dispatcher ends it.  Called from
        the turn's own task, it cancels nothing: the caller unwinds itself.
        """
        if self.detached:
            return
        self.detached = True
        self._ended = True
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()

    def interrupt(self) -> None:
        """Let a take-back blocked on a full worker go on without its slot.

        An interrupted turn only has to reach its interrupt check and stop; it
        does that without a semaphore slot rather than wait for one.
        """
        self._stop_waiting.set()

    async def _give_back_if_all_waiting(self) -> None:
        if self.all_waiting:
            await self._give_back()

    async def _give_back(self) -> None:
        if self._semaphore_held:
            self._semaphore.release()
            self._semaphore_held = False
        if self._gate_held:
            try:
                async with asyncio.timeout(GATE_CALL_TIMEOUT_S):
                    await self._gate.release(
                        self._org_id, self._agent_id, holder=self._gate_holder,
                    )
            except asyncio.CancelledError:
                # The give-back is a single removal of the turn's holder that
                # has most likely landed.  Count the slot as given back; if it
                # had not, the holder stays counted until recovery releases it.
                self._gate_held = False
                raise
            except Exception:
                # Still held: the end of the turn tries again.
                logger.warning(
                    "could not give back the turn slot of org=%s agent=%s",
                    self._org_id, self._agent_id, exc_info=True,
                )
            else:
                self._gate_held = False

    async def _take_back(self) -> None:
        if self._ended:
            return
        if self._uses_gate and not self._gate_held:
            try:
                async with asyncio.timeout(GATE_CALL_TIMEOUT_S):
                    # This turn was admitted already: the tenant's cap governs
                    # new turns, so it takes its slot back whatever the count.
                    self._gate_held = await self._gate.try_acquire(
                        self._org_id, self._agent_id,
                        holder=self._gate_holder, limit=sys.maxsize,
                    )
            except asyncio.CancelledError:
                # The take-back is a single addition of the turn's holder that
                # has most likely landed.  Count the slot as held, so the end
                # of the turn removes the holder: removing one that was never
                # added frees nothing, while one added and never removed stays
                # counted until recovery releases it.
                self._gate_held = True
                raise
            except Exception:
                # Unknown outcome (a timeout or an error), counted as held for
                # the same reason; run on rather than wait on Redis.
                self._gate_held = True
                logger.warning(
                    "could not confirm taking back the turn slot of org=%s agent=%s; "
                    "running on and counting it as held",
                    self._org_id, self._agent_id, exc_info=True,
                )
        if not self._semaphore_held:
            self._semaphore_held = await self._acquire_semaphore()

    async def _acquire_semaphore(self) -> bool:
        """Take a semaphore slot; False if the turn was interrupted or ended first."""
        if self._stop_waiting.is_set():
            return False
        acquire = asyncio.ensure_future(self._semaphore.acquire())
        stop = asyncio.ensure_future(self._stop_waiting.wait())
        try:
            await asyncio.wait({acquire, stop}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            if acquire.done() and not acquire.cancelled():
                # The slot came as this take-back was stopped: give it back.
                self._semaphore.release()
            raise
        finally:
            stop.cancel()
            # A no-op once done; a waiting acquire gives back what it was handed.
            acquire.cancel()
        return acquire.done() and not acquire.cancelled()


current_turn: ContextVar[TurnSlots | None] = ContextVar("surogates_turn_slots", default=None)


def turn_activity() -> contextlib.AbstractAsyncContextManager[None]:
    """The current turn's activity(), or nothing outside a dispatched turn."""
    slots = current_turn.get()
    return slots.activity() if slots is not None else contextlib.nullcontext()


def turn_waiting(*, resumable: bool = False) -> contextlib.AbstractAsyncContextManager[None]:
    """The current turn's waiting(), or nothing outside a dispatched turn."""
    slots = current_turn.get()
    return slots.waiting(resumable=resumable) if slots is not None else contextlib.nullcontext()


def turn_joining() -> contextlib.AbstractAsyncContextManager[None]:
    """The current turn's joining(), or nothing outside a dispatched turn."""
    slots = current_turn.get()
    return slots.joining() if slots is not None else contextlib.nullcontext()


def detach_turn() -> bool:
    """Detach the current turn; False outside a dispatched turn."""
    slots = current_turn.get()
    if slots is None:
        return False
    slots.detach()
    return True


def turn_detached() -> bool:
    """Whether the current turn was detached: what it leaves unfinished is another worker's to resume."""
    slots = current_turn.get()
    return slots is not None and slots.detached
