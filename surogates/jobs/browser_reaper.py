"""Reap registry entries for browsers that are no longer there.

Browsers die without the server hearing about it: a host-level kill, an OOM,
a container reaped from outside all leave the Redis registry entry behind and
never emit ``browser.destroyed``. Every consumer then discovers the corpse
separately and badly — the preview endpoint by erroring, the shell by timing
out, the browser card by advertising a browser nobody can reach, the agent by
answering from memory.

This sweeper makes one place own the truth. Each tick it probes every entry's
REST endpoint with a plain TCP dial, and an entry that fails two consecutive
sweeps is pruned and its session sent the ``browser.destroyed`` event the
server should have emitted in the first place. That event is the point: the
UI reducer, the browser card and the pane already handle it, so honesty
propagates through the event stream with no client-side guessing.

Deliberate boundaries, each learned the hard way:

* **The reaper never destroys containers.** It prunes entries and emits.
  Destroying on a status disagreement is how two workers get into a
  provision/kill ping-pong; pruning a shared entry twice is merely idempotent.
* **A fresh entry is left alone.** Inside the startup window the REST port is
  up but the rest of the stack may not be, and a provision must not be reaped
  from under its first tool call.
* **One failed dial is not a verdict.** Failure counts live in Redis, shared
  across workers, and only consecutive failures prune — a live browser that
  answers on the next sweep gets its strikes cleared.
* **Sweeps are single-flight.** A cheap ``SET NX`` lock keeps N workers from
  probing the same fleet N times; at the current scale one prober is plenty,
  and the lease design in the spec takes over long before that stops holding.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from surogates.browser.registry import BrowserEntry
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_SECONDS = 15.0
# REST readiness is gated at provision but the rest of the stack (CDP, the
# page itself) settles afterwards; nothing younger than this is judged.
PROVISION_GRACE_SECONDS = 30.0
FAILURES_BEFORE_PRUNE = 2
PROBE_TIMEOUT_SECONDS = 1.5
PROBE_CONCURRENCY = 50

LOCK_KEY = "surogates:browser:reaper-lock"
FAILURES_KEY = "surogates:browser:reaper-failures"

EmitEvent = Callable[[str, str, dict], Awaitable[None]]
Probe = Callable[[BrowserEntry], Awaitable[bool]]


async def tcp_probe(entry: BrowserEntry) -> bool:
    """Whether anything is listening at the entry's REST endpoint.

    A TCP dial, not an HTTP request: reachability is the question, and a
    browser that accepts the connection but answers an HTTP request slowly is
    alive — treating slowness as death is exactly the bug that taught us to
    keep this probe dumb.
    """

    parsed = urlparse(entry.rest_url)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, parsed.port),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - any dial failure is "not listening"
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 - already have the answer
        pass
    return True


def _age_seconds(entry: BrowserEntry) -> float:
    try:
        provisioned = entry.provisioned_at
        if provisioned.tzinfo is None:
            provisioned = provisioned.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - provisioned).total_seconds()
    except Exception:  # noqa: BLE001 - a garbage timestamp is an old entry
        return PROVISION_GRACE_SECONDS + 1.0


async def sweep_browser_registry(
    *,
    registry: Any,
    redis: Any,
    emit: EmitEvent,
    probe: Probe = tcp_probe,
) -> int:
    """Probe every registry entry once; prune the repeatedly dead. Returns
    how many entries were pruned."""

    entries: dict[str, BrowserEntry] = await registry.entries()
    if not entries:
        return 0

    semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def bounded_probe(entry: BrowserEntry) -> bool:
        async with semaphore:
            return await probe(entry)

    judged = {
        sid: entry
        for sid, entry in entries.items()
        if _age_seconds(entry) >= PROVISION_GRACE_SECONDS
    }
    if not judged:
        return 0

    results = await asyncio.gather(
        *(bounded_probe(entry) for entry in judged.values()),
        return_exceptions=True,
    )

    pruned = 0
    for (session_id, entry), alive in zip(judged.items(), results):
        if alive is True:
            await redis.hdel(FAILURES_KEY, session_id)
            continue
        failures = await redis.hincrby(FAILURES_KEY, session_id, 1)
        if failures < FAILURES_BEFORE_PRUNE:
            continue
        await registry.delete(session_id)
        await redis.hdel(FAILURES_KEY, session_id)
        pruned += 1
        logger.info(
            "Reaped dead browser %s for session %s after %d failed probes",
            entry.browser_id,
            session_id,
            failures,
        )
        try:
            await emit(
                session_id,
                EventType.BROWSER_DESTROYED.value,
                {"session_id": session_id, "browser_id": entry.browser_id},
            )
        except Exception:  # noqa: BLE001 - the prune already happened; the
            # next consumer to resolve gets an honest answer either way.
            logger.warning(
                "Reaped browser %s but could not emit browser.destroyed",
                session_id,
                exc_info=True,
            )
    return pruned


async def run_browser_reaper_loop(
    *,
    registry: Any,
    redis: Any,
    emit: EmitEvent,
    probe: Probe = tcp_probe,
    interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep forever, one worker at a time."""

    while True:
        try:
            # NX lease slightly shorter than the interval: if this worker dies
            # mid-sweep the lock lapses before the next tick, and two workers
            # racing a tick costs one redundant (idempotent) sweep at worst.
            acquired = await redis.set(
                LOCK_KEY,
                "1",
                nx=True,
                ex=max(1, int(interval_seconds * 0.8)),
            )
            if acquired:
                await sweep_browser_registry(
                    registry=registry, redis=redis, emit=emit, probe=probe,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad sweep must not end the loop
            logger.warning("Browser reaper sweep failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
