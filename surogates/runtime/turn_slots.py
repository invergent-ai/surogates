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
    ) -> None:
        self._semaphore = semaphore
        self._gate = gate
        self._org_id = org_id
        self._agent_id = agent_id
        # A turn that never had a tenant slot never takes one.
        self._uses_gate = gate is not None and gate_held
        self._gate_held = self._uses_gate
        self._semaphore_held = True
        self._active = 0
        self._waiting = 0
        self._ended = False
        # Serialises giving back and taking back; the counters change outside
        # it, so a cancellation cannot leave them wrong.
        self._lock = asyncio.Lock()

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
    async def waiting(self) -> AsyncIterator[None]:
        """Wait on something outside the worker; once all of the turn waits, the slots go back.

        A cancelled wait takes nothing back: whoever carries on (the loop, when
        it stops joining) takes the slots back before it works.
        """
        if _in_wait.get():
            yield
            return
        token = _in_wait.set(True)
        self._waiting += 1
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
            _in_wait.reset(token)
            if not cancelled:
                async with self._lock:
                    if self._waiting < self._active:
                        await self._take_back()

    async def release_owned(self) -> None:
        """End the turn: give back what it still holds, and never take anything again."""
        async with self._lock:
            self._ended = True
            await self._give_back()

    async def _give_back_if_all_waiting(self) -> None:
        if self._active > 0 and self._waiting >= self._active:
            await self._give_back()

    async def _give_back(self) -> None:
        if self._semaphore_held:
            self._semaphore.release()
            self._semaphore_held = False
        if self._gate_held:
            try:
                async with asyncio.timeout(GATE_CALL_TIMEOUT_S):
                    await self._gate.release(self._org_id, self._agent_id)
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
                        self._org_id, self._agent_id, limit=sys.maxsize,
                    )
            except Exception:
                logger.warning(
                    "could not take back the turn slot of org=%s agent=%s; running on without it",
                    self._org_id, self._agent_id, exc_info=True,
                )
            if not self._gate_held:
                # Rather than wait on Redis, run on without it: the cap is a
                # guideline, not a correctness constraint.
                self._uses_gate = False
        if not self._semaphore_held:
            await self._semaphore.acquire()
            self._semaphore_held = True


current_turn: ContextVar[TurnSlots | None] = ContextVar("surogates_turn_slots", default=None)


def turn_activity() -> contextlib.AbstractAsyncContextManager[None]:
    """The current turn's activity(), or nothing outside a dispatched turn."""
    slots = current_turn.get()
    return slots.activity() if slots is not None else contextlib.nullcontext()


def turn_waiting() -> contextlib.AbstractAsyncContextManager[None]:
    """The current turn's waiting(), or nothing outside a dispatched turn."""
    slots = current_turn.get()
    return slots.waiting() if slots is not None else contextlib.nullcontext()


def turn_joining() -> contextlib.AbstractAsyncContextManager[None]:
    """The current turn's joining(), or nothing outside a dispatched turn."""
    slots = current_turn.get()
    return slots.joining() if slots is not None else contextlib.nullcontext()
