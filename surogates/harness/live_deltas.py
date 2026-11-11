"""The ``llm.delta`` events of one model call, coalesced in time.

A streamed reply was written as one ``llm.delta`` event per chunk the
provider sent: one INSERT and one publish per chunk in the worker, and one
read of the log per chunk for every client watching.  At a few dozen
streaming sessions a worker or api process is CPU-bound on them and drains
the model's stream slower than the model writes it.

So consecutive chunks of one kind (``content``, ``reasoning``) become one
event at most every ``coalesce_ms``: the first chunk of a call goes out at
once (time to first token is unchanged), later ones gather while the window
is open and leave when a chunk arrives after it closes, when the kind
changes, or when the stream ends, fails or is interrupted.  The provider's
running reasoning-token count is sent the same way, newest value only.
What the model is sent and what the call returns are not touched; only the
number of ``llm.delta`` events, and how much text each carries, changes.

A coalesced event says how many chunks it joins (``chunks``).  A live
viewer counts reasoning by event: the web chat shows "Thinking... N tokens"
with N its count of reasoning events until the provider reports a token
count.  So a coalesced event that carries reasoning also carries
``reasoning_tokens``: the provider's newest count when it has reported one
in this call, or else the reasoning chunks of the call so far, which is the
number the viewer counted when every chunk was an event.  Replay counts the
chunks of stored events (``SessionStore._restore_reasoning_counts``) the same
way.  With ``coalesce_ms=0`` every event is one chunk and carries neither.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from surogates.session.events import EventType

if TYPE_CHECKING:
    from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)

#: The clock windows are measured on (a module attribute, so tests can move it).
_clock = time.monotonic

#: Default coalescing window.  100 ms is 10 events a second per streaming
#: session, at about the rate text stops looking like it streams, while a
#: model writing 50-150 tokens a second sends one event per 5-15 tokens
#: instead of one per token.  Measured on the headless bench stack with the
#: production model: model requests byte-identical, ``llm.request`` ->
#: ``llm.response`` median 7.0 s -> 0.45 s.
DEFAULT_COALESCE_MS = 100


@dataclass(frozen=True, slots=True)
class DeltaPolicy:
    """How often at most one model call writes an ``llm.delta`` event of one kind.

    ``coalesce_ms=0`` writes one per streamed chunk, as releases before this
    one did.
    """

    coalesce_ms: int = DEFAULT_COALESCE_MS


class LiveDeltas:
    """Writes one model call's ``llm.delta`` events, coalesced by its policy.

    Text held for a window is written when the window closes even if the
    stream goes quiet (a timer, armed while text is held).  The caller must
    :meth:`flush` once the stream is over, however it ended, and before it
    writes the call's ``llm.thinking`` / ``llm.response``: no text is lost,
    and none of the call's deltas lands after them.  Writes are serialised,
    so the events keep the order of the text.
    """

    __slots__ = (
        "_store", "_session_id", "_window", "_meta", "_kind", "_parts", "_tokens", "_last_sent",
        "_reported", "_reasoning_chunks", "_lock", "_timer",
    )

    def __init__(
        self,
        store: SessionStore,
        session_id: UUID,
        *,
        policy: DeltaPolicy | None = None,
        iteration: int,
        turn_id: str | None = None,
        iteration_index: int | None = None,
    ) -> None:
        policy = policy or DeltaPolicy()
        self._store = store
        self._session_id = session_id
        self._window = max(policy.coalesce_ms, 0) / 1000.0
        self._meta = (iteration, turn_id, iteration_index)
        self._kind: str | None = None
        self._parts: list[str] = []
        # A reported count not yet sent, and the newest one reported.
        self._tokens: int | None = None
        self._reported: int | None = None
        # The reasoning chunks of the call so far, across its attempts, as a
        # viewer counting one event per chunk kept counting them.
        self._reasoning_chunks = 0
        self._last_sent = float("-inf")
        self._lock = asyncio.Lock()
        # Writes held text once the window closes, while the stream is quiet.
        self._timer: asyncio.Task | None = None

    async def text(self, kind: str, text: str) -> None:
        """Add *text* of *kind* (``content`` or ``reasoning``)."""
        if not text:
            return
        if self._kind is not None and self._kind != kind:
            await self.flush()
        self._kind = kind
        self._parts.append(text)
        if kind == "reasoning":
            self._reasoning_chunks += 1
        await self.tick()

    async def reasoning_tokens(self, count: int) -> None:
        """Note the provider's running reasoning-token count; the newest is sent."""
        self._tokens = self._reported = count
        await self.tick()

    async def tick(self) -> None:
        """Send what has gathered if the window has closed; called once per chunk."""
        if not self._parts and self._tokens is None:
            return
        wait = self._last_sent + self._window - _clock()
        if wait <= 0:
            await self.flush()
        elif self._timer is None:
            self._timer = asyncio.get_running_loop().create_task(self._later(wait))

    async def send(self, data: dict[str, Any]) -> None:
        """Send *data* as an event of its own, after everything gathered so far."""
        self._stop_timer()
        async with self._lock:
            await self._drain()
            await self._emit(dict(data))

    async def flush(self) -> None:
        """Send everything gathered so far."""
        self._stop_timer()
        async with self._lock:
            await self._drain()

    def _stop_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()  # still asleep: one that woke has cleared ``_timer`` itself

    async def _later(self, wait: float) -> None:
        await asyncio.sleep(wait)
        self._timer = None
        try:
            async with self._lock:
                await self._drain()
        except Exception:  # best-effort, as the stream's own flush on its way out
            logger.debug("Delta flush failed for session %s", self._session_id, exc_info=True)

    async def _drain(self) -> None:
        if self._parts:
            kind, parts = self._kind, self._parts
            self._kind, self._parts = None, []
            data: dict[str, Any] = {kind: "".join(parts), "iteration": self._meta[0]}
            if self._window > 0:
                data["chunks"] = len(parts)
                if kind == "reasoning":
                    data["reasoning_tokens"] = (
                        self._reported if self._reported is not None else self._reasoning_chunks
                    )
            await self._emit(data)
        if self._tokens is not None:
            tokens, self._tokens = self._tokens, None
            await self._emit({"reasoning_tokens": tokens, "iteration": self._meta[0]})

    async def _emit(self, data: dict[str, Any]) -> None:
        from surogates.harness.llm_call import _stamp_turn_meta

        iteration, turn_id, iteration_index = self._meta
        if "iteration" not in data:
            data["iteration"] = iteration
        _stamp_turn_meta(data, iteration=iteration, turn_id=turn_id, iteration_index=iteration_index)
        self._last_sent = _clock()
        await self._store.emit_event(self._session_id, EventType.LLM_DELTA, data)
