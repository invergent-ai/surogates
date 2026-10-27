"""Session-scoped browser pool and lifecycle event bridge."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from surogates.browser.base import (
    BrowserBackend,
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
)
from surogates.browser.client import KernelBrowserClient
from surogates.browser.registry import BrowserEntry, BrowserRegistry
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

EventEmitter = Callable[[str, str, dict[str, Any]], Awaitable[None]]
# Use-time browser-minutes gate. Called with ``org_id`` before a *new*
# pod is provisioned; raises ``BrowserCreditsExhaustedError`` when the
# project is out of minutes. ``None`` disables the gate entirely.
CreditGuard = Callable[[str], Awaitable[None]]
# Per-buyer minutes gate, called after the operator gate on fresh
# provisions with keyword args (session_id, org_id, user_id). Returns
# a billing block to stamp on the pod ({owner_kind, owner_id,
# reservation_id, balance_id}), or ``None`` when the agent is
# unmetered or the session runs on the operator plane — profile
# capture (``browser_setup``) and service-account-principal sessions.
# The guard resolves the session row itself; provisioning is a
# once-per-session event, so the extra read is negligible. Raises
# ``BrowserBudgetExhaustedError`` when the sender's balance is spent.
BudgetGuard = Callable[..., Awaitable[dict[str, str] | None]]


@dataclass(slots=True)
class EnsureResult:
    browser_id: str
    endpoint: BrowserEndpoint
    newly_provisioned: bool
    snapshot_cache: dict[str, dict[str, Any]]


@dataclass(slots=True)
class _Slot:
    browser_id: str
    endpoint: BrowserEndpoint
    snapshot_cache: dict[str, dict[str, Any]]


# How often a reused browser's last use is written to the shared registry.
_TOUCH_EVERY_SECONDS = 60.0

# A session idle this long has its typed secrets dropped even if its browser
# was never destroyed here (it timed out on its own, or the session moved on).
_TYPED_SECRET_TTL = 3600.0


class TypedSecrets:
    """The stored secrets each session's browser was given, kept while that browser lives.

    Whatever a page does with one afterwards (echo it into its text, keep it
    where a script reads it), it is cut out of every browser tool result
    before the model sees it.
    """

    def __init__(self, ttl: float = _TYPED_SECRET_TTL) -> None:
        self._ttl = ttl
        self._by_session: dict[str, tuple[float, set[str]]] = {}

    def add(self, session_id: str, value: str) -> None:
        self._sweep()
        _, secrets = self._by_session.get(session_id, (0.0, set()))
        secrets.add(value)
        self._by_session[session_id] = (time.monotonic(), secrets)

    def of(self, session_id: str) -> list[str]:
        """The session's secrets, longest first; reading them counts as the session's use."""
        self._sweep()
        entry = self._by_session.get(session_id)
        if entry is None:
            return []
        self._by_session[session_id] = (time.monotonic(), entry[1])
        return sorted(entry[1], key=len, reverse=True)

    def forget(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)

    def _sweep(self) -> None:
        cutoff = time.monotonic() - self._ttl
        for session_id in [sid for sid, (used, _) in self._by_session.items() if used < cutoff]:
            del self._by_session[session_id]


class BrowserPool:
    """Worker-local mapping from session id to provisioned browser."""

    def __init__(
        self,
        *,
        backend: BrowserBackend,
        registry: BrowserRegistry,
        event_emitter: EventEmitter | None = None,
        credit_guard: CreditGuard | None = None,
        budget_guard: BudgetGuard | None = None,
        browser_profile_store: Any | None = None,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._emit = event_emitter
        self._credit_guard = credit_guard
        # Public so the worker can wire it after construction — the
        # guard closes over runtime plumbing (platform client, runtime
        # config cache) that is installed later in worker boot.
        self.budget_guard = budget_guard
        # Read by the browser tool layer to inject a session's saved login
        # state at provision; public so handlers reach it via the pool they
        # already hold (avoids threading the store through the executor chain).
        self.browser_profile_store = browser_profile_store
        self.typed_secrets = TypedSecrets()
        self._profile_notes: dict[str, str] = {}
        self._touched: dict[str, float] = {}
        self._mapping: dict[str, _Slot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def ensure(
        self,
        session_id: str,
        org_id: str,
        user_id: str,
        spec: BrowserSpec,
    ) -> EnsureResult:
        lock = await self._session_lock(session_id)
        async with lock:
            slot = self._mapping.get(session_id)
            registered = False
            if slot is None:
                # Shared-runtime sessions can move between workers, so this
                # worker's local map may miss a browser another worker
                # provisioned. Reattach from the shared registry before
                # provisioning a fresh (blank) one — otherwise the session
                # loses its page (lands on the browser's default start page)
                # and the live pod leaks. The status check below validates the
                # reattached slot and reprovisions if the pod is really gone.
                entry = await self._registry.get(session_id)
                if entry is not None and entry.browser_id:
                    slot = _Slot(
                        browser_id=entry.browser_id,
                        endpoint=BrowserEndpoint(
                            rest_url=entry.rest_url,
                            cdp_url=entry.cdp_url,
                            live_view_url=entry.live_view_url,
                        ),
                        snapshot_cache={},
                    )
                    self._mapping[session_id] = slot
                    registered = True
            if slot is not None:
                status = await self._backend.status(slot.browser_id)
                if status == BrowserStatus.RUNNING:
                    # The entry is how viewers find this browser, and a viewer
                    # that could not reach it in time may have pruned it; a
                    # running browser nobody can find is a pane that says
                    # "no browser" for the rest of the session.
                    if not registered and await self._registry.get(session_id) is None:
                        await self._publish(session_id, org_id, user_id, slot)
                    else:
                        await self._touch(session_id)
                    return EnsureResult(
                        browser_id=slot.browser_id,
                        endpoint=slot.endpoint,
                        newly_provisioned=False,
                        snapshot_cache=slot.snapshot_cache,
                    )
                logger.warning(
                    "Browser %s for session %s is %s; reprovisioning",
                    slot.browser_id,
                    session_id,
                    status.value,
                )
                stale_browser_id = slot.browser_id
                await self._backend.destroy(stale_browser_id)
                self._mapping.pop(session_id, None)
                self.typed_secrets.forget(session_id)
                await self._registry.delete(session_id)
                # The replaced pod is gone — surface that to consumers
                # (UI, audit, billing) before the new provision so they
                # see a clean destroyed→provisioned pair instead of two
                # consecutive provisioneds against different pod ids.
                await self._emit_event(
                    session_id,
                    EventType.BROWSER_DESTROYED.value,
                    {"session_id": session_id, "browser_id": stale_browser_id},
                )

            # Use-time enforcement: a new pod is about to start, so this
            # is where browser-minutes are gated. Reusing an
            # already-running pod above is never blocked — only fresh
            # provisions. Raises BrowserCreditsExhaustedError when empty.
            if self._credit_guard is not None:
                await self._credit_guard(org_id)
            # Per-buyer gate, after the operator gate: metered agents
            # hold a chunk of the sender's minutes and the returned
            # billing block rides the spec into the pod labels so the
            # ops monitor extends and settles the hold.
            if self.budget_guard is not None:
                billing = await self.budget_guard(
                    session_id=session_id,
                    org_id=org_id,
                    user_id=user_id,
                )
                if billing:
                    spec.billing = billing

            browser_id, endpoint = await self._backend.provision(
                spec,
                session_id=session_id,
                org_id=org_id,
                user_id=user_id,
            )
            # Inject saved login state into the fresh context *before* the
            # registry publish and the provisioned event, so the agent's first
            # navigation already sees the cookies. Only runs on a new provision.
            if spec.storage_state:
                client = KernelBrowserClient(endpoint.rest_url)
                try:
                    await client.apply_storage_state(spec.storage_state)
                finally:
                    await client.close()
            slot = _Slot(browser_id=browser_id, endpoint=endpoint, snapshot_cache={})
            self._mapping[session_id] = slot
            await self._publish(session_id, org_id, user_id, slot)
            await self._emit_event(
                session_id,
                EventType.BROWSER_PROVISIONED.value,
                {"session_id": session_id, "browser_id": browser_id},
            )
            return EnsureResult(
                browser_id=browser_id,
                endpoint=endpoint,
                newly_provisioned=True,
                snapshot_cache=slot.snapshot_cache,
            )

    async def report_profile_problem(self, session_id: str, profile_id: str, reason: str) -> None:
        """Say, once, that *session_id*'s new browser started without its saved login.

        The session gets an event; the model gets the note on its next
        browser tool result, so it does not act as if it were signed in.
        """
        self._profile_notes[session_id] = (
            f"This browser started logged out: its saved login (browser profile {profile_id}) {reason}. "
            "Do not act as if you were signed in. If the task needs the login, stop and say so in your reply."
        )
        await self._emit_event(
            session_id,
            EventType.BROWSER_PROFILE_UNAVAILABLE.value,
            {"session_id": session_id, "profile_id": profile_id, "reason": reason},
        )

    def take_profile_note(self, session_id: str) -> str | None:
        return self._profile_notes.pop(session_id, None)

    async def _publish(self, session_id: str, org_id: str, user_id: str, slot: _Slot) -> None:
        now = datetime.now(timezone.utc)
        await self._registry.set(
            BrowserEntry(
                session_id=session_id,
                org_id=org_id,
                user_id=user_id,
                rest_url=slot.endpoint.rest_url,
                cdp_url=slot.endpoint.cdp_url,
                live_view_url=slot.endpoint.live_view_url,
                provisioned_at=now,
                browser_id=slot.browser_id,
                last_used_at=now,
            )
        )
        self._touched[session_id] = time.monotonic()

    async def _touch(self, session_id: str) -> None:
        """Record a use in the shared registry, at most once a minute per session."""
        now = time.monotonic()
        if now - self._touched.get(session_id, 0.0) < _TOUCH_EVERY_SECONDS:
            return
        self._touched[session_id] = now
        entry = await self._registry.get(session_id)
        if entry is not None:
            entry.last_used_at = datetime.now(timezone.utc)
            await self._registry.set(entry)

    async def destroy_for_session(self, session_id: str) -> None:
        lock = await self._session_lock(session_id)
        async with lock:
            slot = self._mapping.pop(session_id, None)
            self.typed_secrets.forget(session_id)
            if slot is None:
                # Cold path — the worker either restarted or never owned
                # this session's slot. We still need to clean up the
                # backend and registry, and we still need to emit so the
                # SDK / audit log learn that the browser is gone. The
                # browser_id is unknown here; the event consumer should
                # treat the absence as authoritative.
                registry_entry = await self._registry.get(session_id)
                destroy_for_session = getattr(
                    self._backend, "destroy_for_session", None
                )
                if callable(destroy_for_session):
                    await destroy_for_session(session_id)
                elif registry_entry is not None and registry_entry.browser_id:
                    # A backend that tears down only by its own handle
                    # (fleet): the entry another worker wrote carries it.
                    await self._backend.destroy(registry_entry.browser_id)
                await self._registry.delete(session_id)
                if registry_entry is not None:
                    await self._emit_event(
                        session_id,
                        EventType.BROWSER_DESTROYED.value,
                        {"session_id": session_id, "browser_id": None},
                    )
                return
            await self._backend.destroy(slot.browser_id)
            await self._registry.delete(session_id)
            await self._emit_event(
                session_id,
                EventType.BROWSER_DESTROYED.value,
                {"session_id": session_id, "browser_id": slot.browser_id},
            )

        async with self._global_lock:
            self._locks.pop(session_id, None)
            self._touched.pop(session_id, None)

    async def destroy_all(self) -> None:
        async with self._global_lock:
            session_ids = list(self._mapping.keys())
        for session_id in session_ids:
            try:
                await self.destroy_for_session(session_id)
            except Exception:
                logger.exception("Error destroying browser for session %s", session_id)

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._global_lock:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    async def _emit_event(
        self, session_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        if self._emit is not None:
            await self._emit(session_id, event_type, data)
