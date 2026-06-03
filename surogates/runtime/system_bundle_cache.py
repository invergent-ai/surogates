"""Process-singleton cache for the shared ``platform/system-skills`` bundle.

The system bundle is a single Hub repository whose root IS the
platform-skill catalog (``<skill-name>/SKILL.md`` + optional
``references/`` / ``templates/`` / ``scripts/`` / ``assets/`` subtrees,
no nested ``skills/`` directory).  Every shared-runtime agent in the
cluster reads from the same snapshot, so the cache is NOT keyed by
``agent_id`` — it holds at most one ``AgentFileBundle`` accessor at any
time.

Contract parity with :class:`~surogates.runtime.bundle_cache.FileBundleCache`:

* ``async get() -> AgentFileBundle`` — fetch (and cache) the current
  snapshot.
* ``invalidate(identifier=None) -> None`` — drop the cached slot.  The
  ``identifier`` argument is accepted for parity with the invalidator
  dispatch surface but ignored because the cache is a single slot.

Loader exception policy: a failure during ``get()`` MUST NOT be cached.
A transient Hub outage at session-start would otherwise poison every
subsequent session until the next ``invalidate()`` arrived.

Concurrent ``get()`` callers during the first miss serialise on an
``asyncio.Lock`` and converge on a single loader invocation.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

__all__ = ["SYSTEM_SKILLS_REPO", "SystemBundleCache"]


# Hub repository holding the global system-skills catalog.  Encoded as
# a literal here AND in ``surogate_ops.core.hub.system_skills`` — the
# two sides do not import each other.  Keep them in sync.
SYSTEM_SKILLS_REPO = "platform/system-skills"


class SystemBundleCache:
    """Single-slot async cache wrapping a bundle loader.

    The loader is a zero-argument async callable that resolves the
    current ``platform/system-skills`` snapshot (typically by querying
    Hub for the latest ``v*`` tag and constructing an
    :class:`~surogates.runtime.bundle_accessor.AgentFileBundle`).
    """

    def __init__(
        self,
        loader: Callable[[], Awaitable[Any]],
    ) -> None:
        self._loader = loader
        self._bundle: Any | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Any:
        """Return the cached bundle, fetching it on first use.

        Re-fetches after every :meth:`invalidate` call.  Loader
        exceptions propagate verbatim and are NOT memoised so the next
        call retries.
        """

        # Fast path without lock acquisition.
        if self._bundle is not None:
            return self._bundle

        async with self._lock:
            # Re-check under the lock — another task may have populated
            # the slot while we were waiting.
            if self._bundle is not None:
                return self._bundle
            # Important: assign ONLY on success.  An exception leaves
            # the slot empty so the next ``get()`` retries.
            bundle = await self._loader()
            self._bundle = bundle
            return bundle

    def invalidate(self, identifier: str | None = None) -> None:
        """Drop the cached bundle.

        ``identifier`` is the channel-suffix passed by the invalidator
        dispatch loop (e.g. the new tag).  This cache ignores it
        because the slot is global and the loader always re-resolves
        the latest tag on the next ``get()``.
        """

        # The reference to ``identifier`` keeps the linters quiet and
        # documents that the kwarg is part of the dispatch contract.
        del identifier
        self._bundle = None
