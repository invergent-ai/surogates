"""Session-aware sandbox pool with health-check and auto-reprovision.

``SandboxPool`` maps *session_id* to *sandbox_id*, ensuring each session gets
at most one sandbox at a time and transparently reprovisioning if the
underlying sandbox enters an unhealthy state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from surogates.sandbox.base import SandboxSpec, SandboxStatus

if TYPE_CHECKING:
    from surogates.sandbox.base import Sandbox

logger = logging.getLogger(__name__)


def sandbox_session_key(session: Any) -> str:
    """Return the :class:`SandboxPool` key for *session*.

    Delegation children share their root ancestor's sandbox — the
    sub-agent is doing part of the parent's work, so giving it a fresh
    empty workspace defeats the purpose.  Every child of a delegation
    chain resolves to the *ultimate* root so parents, children, and
    grandchildren all land on the same pool entry.

    The resolution uses a cached ``sandbox_root_session_id`` that the
    delegate tool stamps into the child's ``session.config`` at
    creation time (O(1) lookup with no DB hop).  When the key is
    absent — either because the session is a root, or because it was
    created outside the delegate path — we fall back to
    ``parent_id or session.id``, which covers single-level delegations
    emitted before the root cache was introduced.

    ``destroy_for_session`` still passes the child's own id (a no-op
    against the pool when the child never provisioned its own
    sandbox), so child cleanup leaves the shared workspace intact.
    """
    config = getattr(session, "config", None) or {}
    root = config.get("sandbox_root_session_id")
    if root:
        return str(root)
    parent = getattr(session, "parent_id", None)
    return str(parent or session.id)


class SandboxPool:
    """Manages the ``session_id -> sandbox_id`` mapping.

    Thread-safety is achieved via one :class:`asyncio.Lock` per session so
    that concurrent requests for the *same* session are serialised while
    requests for *different* sessions proceed in parallel.
    """

    def __init__(self, backend: Sandbox) -> None:
        self._backend = backend
        # session_id -> sandbox_id
        self._mapping: dict[str, str] = {}
        # session_id -> SandboxSpec (kept for reprovisioning)
        self._specs: dict[str, SandboxSpec] = {}
        # Per-session locks to serialise provisioning and execution.
        self._locks: dict[str, asyncio.Lock] = {}
        # Guard for mutating the dicts themselves.
        self._global_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure(self, session_id: str, spec: SandboxSpec) -> str:
        """Return the sandbox_id for *session_id*, provisioning if needed.

        If the existing sandbox is not healthy (status != ``RUNNING``), it is
        destroyed and a fresh one is provisioned.
        """
        lock = await self._session_lock(session_id)
        async with lock:
            sandbox_id = self._mapping.get(session_id)

            if sandbox_id is not None:
                # Health-check the existing sandbox.
                status = await self._backend.status(sandbox_id)
                if status == SandboxStatus.RUNNING:
                    return sandbox_id
                # Stale or failed -- clean up and reprovision.
                logger.warning(
                    "Sandbox %s for session %s has status %s; reprovisioning",
                    sandbox_id,
                    session_id,
                    status.value,
                )
                await self._backend.destroy(sandbox_id)

            # Provision a new sandbox.
            sandbox_id = await self._backend.provision(spec)
            async with self._global_lock:
                self._mapping[session_id] = sandbox_id
                self._specs[session_id] = spec
            logger.info(
                "Session %s mapped to sandbox %s", session_id, sandbox_id
            )
            return sandbox_id

    async def execute(self, session_id: str, name: str, input: str) -> str:
        """Execute a command in the sandbox belonging to *session_id*.

        Raises :class:`ValueError` if the session has no associated sandbox.
        """
        lock = await self._session_lock(session_id)
        async with lock:
            sandbox_id = self._mapping.get(session_id)
            if sandbox_id is None:
                raise ValueError(
                    f"No sandbox provisioned for session {session_id}"
                )
            return await self._backend.execute(sandbox_id, name, input)

    async def destroy_for_session(self, session_id: str) -> None:
        """Destroy the sandbox for *session_id* and remove the mapping."""
        lock = await self._session_lock(session_id)
        async with lock:
            sandbox_id = self._mapping.pop(session_id, None)
            self._specs.pop(session_id, None)
            if sandbox_id is not None:
                await self._backend.destroy(sandbox_id)
                logger.info(
                    "Destroyed sandbox %s for session %s",
                    sandbox_id,
                    session_id,
                )

        # Clean up the per-session lock to prevent unbounded growth.
        async with self._global_lock:
            self._locks.pop(session_id, None)

    async def destroy_all(self) -> None:
        """Tear down every sandbox managed by this pool.

        Intended for graceful shutdown.
        """
        async with self._global_lock:
            session_ids = list(self._mapping.keys())

        for session_id in session_ids:
            try:
                await self.destroy_for_session(session_id)
            except Exception:
                logger.exception(
                    "Error destroying sandbox for session %s", session_id
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session lock, creating one if it does not exist."""
        async with self._global_lock:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock
