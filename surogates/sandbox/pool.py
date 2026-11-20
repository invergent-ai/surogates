"""Session-aware sandbox pool with health-check and auto-reprovision.

``SandboxPool`` maps *session_id* to *sandbox_id*, ensuring each session gets
at most one sandbox at a time and transparently reprovisioning if the
underlying sandbox enters an unhealthy state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from surogates.sandbox.base import SandboxSpec, SandboxStatus, SandboxUnavailableError

if TYPE_CHECKING:
    from surogates.sandbox.base import Sandbox

logger = logging.getLogger(__name__)

#: Session-image keys whose "made new" marks the pool keeps for sessions it holds no sandbox for.
_IMAGE_MARKS_KEPT = 5000


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
        # Threads whose copy this pool has just made from the real files,
        # not yet asked about: whether that loses work is the session's log's to say.
        self._fresh: set[str] = set()
        # Sessions in their own image whose container the backend has just made new from the image, not yet asked
        # about (``image_new``): work the session did in a container before is not in it.
        # Session-image keys: how many times this pool made the key's container new from its image, and, per
        # session sharing it (the root, its sub-agents), the last of those it was told about.
        self._image_made: dict[str, int] = {}
        self._image_told: dict[str, dict[str, int]] = {}
        # session_id -> when its sandbox was last asked for or a call in it ended (monotonic), and the calls in it now.
        self._used: dict[str, float] = {}
        self._busy: dict[str, int] = {}
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure(self, session_id: str, spec: SandboxSpec) -> str:
        """Return the sandbox_id for *session_id*, provisioning if needed.

        If the existing sandbox is not healthy (status != ``RUNNING``), it is
        destroyed and a fresh one is provisioned.

        A *spec* for a thread's copy is refused a pod that holds none: a
        thread's steps never run on, and its Stop never restores over, the
        real files.
        """
        # Imported here: surogates.devices.sandbox imports this module.
        from surogates.devices.sandbox import DEVICE_SANDBOX_ID, is_device_owner

        if is_device_owner(session_id):
            # A session on the user's computer never gets a cloud sandbox,
            # and an offline computer is not a dead sandbox to replace.
            return DEVICE_SANDBOX_ID

        lock = await self._session_lock(session_id)
        async with lock:
            sandbox_id = self._mapping.get(session_id)

            if sandbox_id is not None:
                # Health-check the existing sandbox.
                status = await self._backend.status(sandbox_id)
                if status == SandboxStatus.RUNNING:
                    if "PROJECT_DIR" in spec.env and not self.holds_copy(session_id):
                        raise SandboxUnavailableError(
                            "This thread's sandbox holds no copy of the project's files, so its steps cannot run there",
                        )
                    self._used[session_id] = time.monotonic()
                    return sandbox_id
                # Stale or failed -- clean up and reprovision.
                logger.warning(
                    "Sandbox %s for session %s has status %s; reprovisioning",
                    sandbox_id,
                    session_id,
                    status.value,
                )
                forget = getattr(self._backend, "forget", None)
                if self._in_its_image(session_id) and forget is not None:
                    # Its container is the session's work, and another worker may be calling into it: never stopped
                    # here.  The provision takes it over, starts it again, or makes a new one.
                    forget(sandbox_id)
                else:
                    await self._backend.destroy(sandbox_id)

            # Provision a new sandbox.
            sandbox_id = await self._backend.provision(spec)
            async with self._global_lock:
                self._mapping[session_id] = sandbox_id
                self._specs[session_id] = spec
                self._used[session_id] = time.monotonic()
                if "PROJECT_DIR" in spec.env:
                    self._fresh.add(session_id)
                created = getattr(self._backend, "created", None)
                if spec.session_image and created is not None and created(sandbox_id):
                    self._image_made[session_id] = self._image_made.get(session_id, 0) + 1
            logger.info(
                "Session %s mapped to sandbox %s", session_id, sandbox_id
            )
            return sandbox_id

    async def execute(self, session_id: str, name: str, input: str, *, timeout: float | None = None) -> str:
        """Execute a command in the sandbox belonging to *session_id*.

        The session lock is held only while resolving the sandbox id —
        not across the backend call.  The harness already decides which
        tool calls may run concurrently (``StreamingToolExecutor``), so
        holding the lock here would serialize them for no benefit.  A
        ``destroy_for_session`` racing an in-flight call makes that call
        fail exactly like a pod dying mid-execution, which the caller
        already handles.

        *timeout* bounds this call in place of the sandbox's own.

        Raises :class:`ValueError` if the session has no associated sandbox.
        """
        from surogates.devices.sandbox import is_device_owner, refusal

        if is_device_owner(session_id):
            # Outside a tool call there is no invocation to journal the work under.
            return refusal(name)

        lock = await self._session_lock(session_id)
        async with lock:
            sandbox_id = self._mapping.get(session_id)
            if sandbox_id is not None:
                # Counted under the lock: a reap that takes the lock next sees the call.
                self._busy[session_id] = self._busy.get(session_id, 0) + 1
        if sandbox_id is None:
            raise ValueError(
                f"No sandbox provisioned for session {session_id}"
            )
        try:
            return await self._backend.execute(sandbox_id, name, input, timeout=timeout)
        finally:
            left = self._busy.get(session_id, 1) - 1
            if left > 0:
                self._busy[session_id] = left
            else:
                self._busy.pop(session_id, None)
            if session_id in self._mapping:
                self._used[session_id] = time.monotonic()

    def copy_fresh(self, session_id: str) -> bool:
        """Whether this pool has just made *session_id*'s copy from the real files; asked once."""
        if session_id not in self._fresh:
            return False
        self._fresh.discard(session_id)
        return True

    def sandbox_of(self, session_id: str) -> str | None:
        """The sandbox *session_id* maps to now; None when it has none."""
        return self._mapping.get(session_id)

    def holds_copy(self, session_id: str) -> bool:
        """Whether *session_id* has a pod now, and it is a thread's, over its copy."""
        spec = self._specs.get(session_id)
        return spec is not None and "PROJECT_DIR" in spec.env

    def image_new(self, session_id: str, asking: str | None = None) -> bool:
        """Whether the own-image container of the sandbox key *session_id* was made new from its image (not taken
        over, not started again) since the session *asking* (default: the key's own) last asked: whatever that
        session did in a container before is not in it.  Each session sharing the key asks for itself."""
        made = self._image_made.get(session_id, 0)
        told = self._image_told.setdefault(session_id, {}) if made else {}
        who = asking or session_id
        if not made or told.get(who, 0) >= made:
            return False
        told[who] = made
        return True

    def _drop_image_marks(self, key: str) -> None:
        self._image_made.pop(key, None)
        self._image_told.pop(key, None)

    async def release_for_session(self, session_id: str, *, only: str | None = None) -> str | None:
        """Detach the sandbox from *session_id*, returning its id.

        With *only*, nothing is detached unless *session_id* still maps to
        that sandbox: a later one is another turn's.

        In-memory only, so it is fast enough to stay on a latency-
        sensitive path. Callers that then destroy the returned sandbox in
        the background get the ordering that matters: once this returns,
        no later turn can resolve the session to a pod that is about to
        disappear.
        """
        lock = await self._session_lock(session_id)
        async with lock:
            if only is not None and self._mapping.get(session_id) != only:
                return None
            self._specs.pop(session_id, None)
            self._fresh.discard(session_id)
            self._used.pop(session_id, None)
            self._drop_image_marks(session_id)
            return self._mapping.pop(session_id, None)

    async def reap_at_rest(
        self, at_rest: Callable[[str], Awaitable[bool]], *, idle_seconds: float, image_idle_seconds: float | None = None,
    ) -> list[str]:
        """Destroy the sandboxes this pool holds for sessions at rest, unused for *idle_seconds*; their keys.

        The safety net behind the turn ends' teardown.  A turn end leaves the
        sandbox up while an agent below the session still runs in it, and the
        turn end the agent's report brings may run on another worker, whose
        sweep spares this worker's live container: nothing else ever lets
        this one go.  *at_rest* says whether a session key's session and the
        agents below it are all at rest.  Never a sandbox with a call in
        flight, nor a thread's pod over its copy, whose keep or landing
        decides what becomes of its work.  The destroy runs under the
        session's lock: a turn that asks for the sandbox meanwhile waits,
        then gets a fresh one that no sweep of this reap can take.
        """
        reaped: list[str] = []
        unsure = 0
        for key, sandbox_id in list(self._mapping.items()):
            if not self._idle(key, idle_seconds) or self.holds_copy(key) or self._in_its_image(key):
                # A session's own image is the backend's to reap, by the container's own clock (below): another
                # worker may work in it, which this pool's clock never sees.
                continue
            try:
                if not await at_rest(key):
                    continue
            except Exception:
                unsure += 1
                if unsure == 1:
                    logger.debug("Could not tell whether session %s is at rest", key, exc_info=True)
                continue
            lock = await self._session_lock(key)
            async with lock:
                # Asked for, or a call started, since the look: a turn has it again.
                if self._mapping.get(key) != sandbox_id or not self._idle(key, idle_seconds):
                    continue
                spec, used = self._specs.pop(key, None), self._used.pop(key, None)
                self._fresh.discard(key)
                self._mapping.pop(key, None)
                destroy = asyncio.ensure_future(self._destroy_reaped(sandbox_id, key))
                try:
                    # Shielded: a reaper cancelled mid-destroy (shutdown) waits for it, nothing is left half done.
                    await asyncio.shield(destroy)
                except asyncio.CancelledError:
                    with contextlib.suppress(Exception):
                        await destroy
                    raise
                except Exception:
                    logger.warning("Could not destroy sandbox %s of session %s at rest; the next round tries again",
                                   sandbox_id, key, exc_info=True)
                    # Kept, so it is asked for again: a pod whose delete failed has no other sweep to go by.
                    self._mapping[key] = sandbox_id
                    if spec is not None:
                        self._specs[key] = spec
                    if used is not None:
                        self._used[key] = used
                    continue
            reaped.append(key)
        if unsure:
            logger.warning("Could not tell whether %d sessions are at rest; their sandboxes stay", unsure)
        reap_images = getattr(self._backend, "reap_images", None)
        image_idle = idle_seconds if image_idle_seconds is None else image_idle_seconds
        if reap_images is not None and image_idle > 0:
            for key, _container in await reap_images(at_rest, idle_seconds=image_idle):
                reaped.append(key)
                if key in self._mapping:
                    await self._forget_if_gone(key)
        # This pool's own entries of image sandboxes whose container is gone (reaped by another worker, removed by
        # hand): dropped, so nothing of them stays in memory.
        for key in [k for k in list(self._mapping) if self._in_its_image(k)]:
            await self._forget_if_gone(key)
        return reaped

    async def _forget_if_gone(self, key: str) -> None:
        """Drop *key*'s mapping when its own-image sandbox no longer runs (reaped, maybe by another worker, or
        stopped): its next call takes it over, starts it again or makes a new one.  Nothing is destroyed here."""
        lock = await self._session_lock(key)
        async with lock:
            sandbox_id = self._mapping.get(key)
            if sandbox_id is None or self._busy.get(key):
                return
            gone = getattr(self._backend, "gone", None)
            try:
                if gone is not None:
                    if not await gone(sandbox_id):
                        return
                elif await self._backend.status(sandbox_id) == SandboxStatus.RUNNING:
                    return
            except Exception:
                return
            forget = getattr(self._backend, "forget", None)
            if forget is not None:
                forget(sandbox_id)
            self._mapping.pop(key, None)
            self._specs.pop(key, None)
            self._used.pop(key, None)
            # The marks stay: a container taken over or started again next is no news; a new one is.  Bounded: past
            # a few thousand keys, those of sessions this pool holds no sandbox for go.
            if len(self._image_made) > _IMAGE_MARKS_KEPT:
                for gone_key in [k for k in self._image_made if k not in self._mapping]:
                    self._drop_image_marks(gone_key)

    def _in_its_image(self, key: str) -> bool:
        spec = self._specs.get(key)
        return spec is not None and bool(spec.session_image)

    def _idle(self, key: str, idle_seconds: float) -> bool:
        """Whether *key*'s sandbox has no call in flight and went unused for *idle_seconds*."""
        used = self._used.get(key)
        return not self._busy.get(key) and used is not None and time.monotonic() - used >= idle_seconds

    async def _destroy_reaped(self, sandbox_id: str, key: str) -> None:
        await self._backend.destroy(sandbox_id)
        logger.info("Reaped sandbox %s of session %s at rest", sandbox_id, key)
        # The backend's sweep of the session's containers too: those of workers that are gone.
        sweep = getattr(self._backend, "destroy_for_session", None)
        if sweep is not None:
            await sweep(key)

    def start_reaping(
        self, at_rest: Callable[[str], Awaitable[bool]], *, every: float = 60.0, idle_seconds: float = 300.0,
        image_idle_seconds: float | None = None,
    ) -> None:
        """Run :meth:`reap_at_rest` every *every* seconds; *idle_seconds* 0 or less (or *every*) runs none, unless
        *image_idle_seconds* (the sessions in their own image) is above 0."""
        image_idle = idle_seconds if image_idle_seconds is None else image_idle_seconds
        if (idle_seconds <= 0 and image_idle <= 0) or every <= 0 or (self._reaper is not None and not self._reaper.done()):
            return
        self._reaper = asyncio.create_task(
            self._reap_every(at_rest, every, idle_seconds, image_idle), name="sandbox-reaper",
        )

    async def _reap_every(
        self, at_rest: Callable[[str], Awaitable[bool]], every: float, idle_seconds: float, image_idle: float,
    ) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                reaped = await self.reap_at_rest(
                    at_rest, idle_seconds=idle_seconds if idle_seconds > 0 else float("inf"),
                    image_idle_seconds=image_idle,
                )
            except Exception:
                logger.warning("The sandbox reaper's round failed", exc_info=True)
                continue
            if reaped:
                logger.info("Reaped the sandboxes of %d sessions at rest: %s", len(reaped), ", ".join(reaped))

    async def stop_reaping(self) -> None:
        """Stop the reaper, its round in flight finished or cut where nothing is left half done."""
        reaper, self._reaper = self._reaper, None
        if reaper is None:
            return
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reaper

    async def execute_released(self, sandbox_id: str, name: str, input: str, *, timeout: float | None = None) -> str:
        """Execute a command in a sandbox :meth:`release_for_session` detached, before it is destroyed.

        No session resolves to it any more, so no turn's step runs beside
        this one: its last work, such as the day's pruning of a project's
        history, done after its turn has reported.
        """
        return await self._backend.execute(sandbox_id, name, input, timeout=timeout)

    async def expire_released(self, sandbox_id: str, seconds: float) -> None:
        """Have a sandbox :meth:`release_for_session` detached end by itself within *seconds*, where the backend can.

        For one at its last work in no session's keeping: a delete that is
        never answered then leaves it that long, not the life it was made with.
        """
        expire = getattr(self._backend, "expire", None)
        if expire is not None:
            await expire(sandbox_id, seconds)

    async def destroy_released(
        self, sandbox_id: str | None, session_id: str, *, alone: bool = False,
    ) -> None:
        """Tear down a sandbox already detached by :meth:`release_for_session`.

        This is the slow half -- deleting a pod is a round trip to the
        cluster -- and it no longer needs the session lock, because the
        mapping is gone and nothing can resolve to this sandbox any more.

        *alone* destroys that sandbox and nothing else of its session's:
        for one that outlived its wake, whose session may hold another
        sandbox, and its lock, by now.
        """
        if sandbox_id is not None:
            await self._backend.destroy(sandbox_id)
            logger.info(
                "Destroyed sandbox %s for session %s", sandbox_id, session_id,
            )
        if alone:
            return

        # Optional backend-level reap (label-based), independent of the
        # mapping above.
        backend_reap = getattr(self._backend, "destroy_for_session", None)
        if backend_reap is not None:
            await backend_reap(session_id)

        # Clean up the per-session lock to prevent unbounded growth.
        async with self._global_lock:
            self._locks.pop(session_id, None)

    async def destroy_for_session(self, session_id: str) -> None:
        """Destroy the sandbox for *session_id* and remove the mapping.

        Also calls the backend's optional ``destroy_for_session`` (Docker)
        so stale containers/pods labelled for this session are reaped even
        when this pool has no in-memory mapping (e.g. after a worker
        restart).
        """
        sandbox_id = await self.release_for_session(session_id)
        await self.destroy_released(sandbox_id, session_id)

    async def stop_image_session(self, session_id: str) -> None:
        """Stop *session_id*'s own-image container wherever it runs (a Stop, ``/clear``): its commands end, its
        files stay for the session's next call (the backend's, when it has session images)."""
        stop = getattr(self._backend, "stop_image_of_session", None)
        if stop is not None:
            await stop(session_id)

    async def destroy_all(self) -> None:
        """Tear down every sandbox managed by this pool.

        Intended for graceful shutdown.
        """
        await self.stop_reaping()
        async with self._global_lock:
            session_ids = list(self._mapping.keys())

        for session_id in session_ids:
            if self._in_its_image(session_id):
                # Another worker may be working in it: a worker's shutdown never ends a session's own image.
                await self.release_for_session(session_id)
                continue
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
