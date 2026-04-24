"""Website-agent CRUD and publishable-key verification.

A :class:`WebsiteAgent` row is the configuration for one public-website
embed of the agent -- CORS allow-list, publishable key, tool allow-list,
system prompt, caps.  Admin tooling (``surogate-ops``) manages these
rows through this module's Python API; there is no HTTP admin surface.

Publishable-key format::

    surg_wk_<44 base64url characters>

The raw key is returned to the caller exactly once on
:meth:`WebsiteAgentStore.create`.  Only a SHA-256 hex digest is stored.
The ``surg_wk_`` prefix distinguishes publishable keys from
service-account keys (``surg_sk_``) at the auth boundary: publishable
keys authenticate the bootstrap request that exchanges them for a
short-lived, origin-bound session cookie, while service-account keys
authenticate server-to-server pipeline calls.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import WebsiteAgent

logger = logging.getLogger(__name__)

__all__ = [
    "PUBLISHABLE_KEY_PREFIX",
    "IssuedWebsiteAgent",
    "ResolvedWebsiteAgent",
    "WebsiteAgentStore",
    "generate_publishable_key",
    "hash_publishable_key",
    "is_publishable_key",
]


PUBLISHABLE_KEY_PREFIX = "surg_wk_"
_DISPLAY_PREFIX_LEN = len(PUBLISHABLE_KEY_PREFIX) + 8
_SECRET_BYTES = 33
# Short TTL so publishable-key revocation and origin-list changes
# converge across replicas without per-request DB round-trips.  Session
# cookies are issued with their own independent TTL (~1h); this cache
# only affects the bootstrap path.
_CACHE_TTL_SECONDS: float = 30.0


def is_publishable_key(token: str) -> bool:
    """Return True when *token* carries the publishable-key prefix."""
    return token.startswith(PUBLISHABLE_KEY_PREFIX)


def generate_publishable_key() -> str:
    """Return a freshly minted, high-entropy publishable key."""
    return PUBLISHABLE_KEY_PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


def hash_publishable_key(token: str) -> str:
    """Return the SHA-256 hex digest of *token* for storage and lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedWebsiteAgent:
    """Cached projection of the row the bootstrap path needs.

    Carries every field that either feeds the session cookie claims
    (``org_id``, ``id``) or gates the bootstrap itself
    (``allowed_origins``, ``enabled``, ``tool_allow_list``,
    ``system_prompt``, caps).  Enough to finish the bootstrap request
    without a second DB lookup.
    """

    id: UUID
    org_id: UUID
    name: str
    allowed_origins: tuple[str, ...]
    tool_allow_list: tuple[str, ...]
    system_prompt: str | None
    model: str | None
    skill_pins: tuple[str, ...]
    session_message_cap: int
    session_token_cap: int
    session_idle_minutes: int
    enabled: bool


@dataclass(frozen=True)
class IssuedWebsiteAgent:
    """A website agent freshly created by :meth:`WebsiteAgentStore.create`.

    The raw *publishable_key* is present here and nowhere else — the
    ops operator that created the row must embed it in their website's
    JS; it cannot be recovered later.
    """

    id: UUID
    org_id: UUID
    name: str
    publishable_key: str
    publishable_key_prefix: str
    allowed_origins: tuple[str, ...]
    created_at: datetime


class _TTLCache:
    """Tiny TTL cache for resolved publishable-key rows."""

    __slots__ = ("_entries", "_ttl")

    def __init__(self, ttl_seconds: float) -> None:
        self._entries: dict[str, tuple[float, ResolvedWebsiteAgent]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> ResolvedWebsiteAgent | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._entries.pop(key, None)
            return None
        return value

    def set(self, key: str, value: ResolvedWebsiteAgent) -> None:
        self._entries[key] = (time.monotonic() + self._ttl, value)

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


_KEY_CACHE: _TTLCache = _TTLCache(_CACHE_TTL_SECONDS)
_ID_CACHE: _TTLCache = _TTLCache(_CACHE_TTL_SECONDS)


def _reset_caches() -> None:
    """Clear the website-agent auth caches.  Exposed for tests."""
    _KEY_CACHE.clear()
    _ID_CACHE.clear()


def _normalize_origin(origin: str) -> str:
    """Normalise *origin* to its canonical ``scheme://host[:port]`` form.

    Browsers send the ``Origin`` header without a trailing slash and in
    lowercase scheme; config files written by humans often diverge.
    Normalising on both sides prevents mismatches that a wildcard would
    otherwise paper over.
    """
    return origin.strip().rstrip("/").lower()


def _row_to_resolved(row: WebsiteAgent) -> ResolvedWebsiteAgent:
    return ResolvedWebsiteAgent(
        id=row.id,
        org_id=row.org_id,
        name=row.name,
        allowed_origins=tuple(_normalize_origin(o) for o in row.allowed_origins or []),
        tool_allow_list=tuple(row.tool_allow_list or []),
        system_prompt=row.system_prompt,
        model=row.model,
        skill_pins=tuple(row.skill_pins or []),
        session_message_cap=row.session_message_cap,
        session_token_cap=row.session_token_cap,
        session_idle_minutes=row.session_idle_minutes,
        enabled=row.enabled,
    )


class WebsiteAgentStore:
    """CRUD for ``website_agents``.

    Stateless; every method opens its own session on the supplied
    ``async_sessionmaker``.  Secrets are hashed before they reach the
    database and the module-level caches invalidate on mutation so a
    disable or origin-list change converges on the issuing process
    immediately (peer processes within the TTL window).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------
    # Mutations (ops-facing)
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        org_id: UUID,
        name: str,
        allowed_origins: list[str],
        tool_allow_list: list[str] | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        description: str | None = None,
        skill_pins: list[str] | None = None,
        session_message_cap: int = 0,
        session_token_cap: int = 0,
        session_idle_minutes: int = 30,
    ) -> IssuedWebsiteAgent:
        """Create a new website agent and return its raw publishable key.

        The key is returned exactly once -- subsequent lookups can only
        recover the hash.  ``allowed_origins`` entries are normalised
        (``scheme://host[:port]``) before storage so runtime comparisons
        don't need to renormalise.
        """
        if not allowed_origins:
            raise ValueError(
                "allowed_origins must contain at least one entry; a website "
                "agent with no origin list cannot authenticate any browser."
            )

        key = generate_publishable_key()
        normalized = [_normalize_origin(o) for o in allowed_origins]
        row = WebsiteAgent(
            org_id=org_id,
            name=name,
            description=description,
            publishable_key_hash=hash_publishable_key(key),
            publishable_key_prefix=key[:_DISPLAY_PREFIX_LEN],
            allowed_origins=normalized,
            tool_allow_list=list(tool_allow_list or []),
            system_prompt=system_prompt,
            model=model,
            skill_pins=list(skill_pins or []),
            session_message_cap=session_message_cap,
            session_token_cap=session_token_cap,
            session_idle_minutes=session_idle_minutes,
        )
        async with self._sf() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)

        return IssuedWebsiteAgent(
            id=row.id,
            org_id=row.org_id,
            name=row.name,
            publishable_key=key,
            publishable_key_prefix=row.publishable_key_prefix,
            allowed_origins=tuple(normalized),
            created_at=row.created_at,
        )

    async def update(
        self,
        agent_id: UUID,
        *,
        allowed_origins: list[str] | None = None,
        tool_allow_list: list[str] | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        description: str | None = None,
        skill_pins: list[str] | None = None,
        session_message_cap: int | None = None,
        session_token_cap: int | None = None,
        session_idle_minutes: int | None = None,
        enabled: bool | None = None,
    ) -> ResolvedWebsiteAgent | None:
        """Partially update a website agent.

        ``None`` means "leave this field unchanged".  Invalidates both
        auth caches so changes (especially ``enabled=False`` or an
        ``allowed_origins`` shrink) take effect on the next bootstrap
        in this process.
        """
        values: dict[str, Any] = {}
        if allowed_origins is not None:
            values["allowed_origins"] = [_normalize_origin(o) for o in allowed_origins]
        if tool_allow_list is not None:
            values["tool_allow_list"] = list(tool_allow_list)
        if system_prompt is not None:
            values["system_prompt"] = system_prompt
        if model is not None:
            values["model"] = model
        if description is not None:
            values["description"] = description
        if skill_pins is not None:
            values["skill_pins"] = list(skill_pins)
        if session_message_cap is not None:
            values["session_message_cap"] = session_message_cap
        if session_token_cap is not None:
            values["session_token_cap"] = session_token_cap
        if session_idle_minutes is not None:
            values["session_idle_minutes"] = session_idle_minutes
        if enabled is not None:
            values["enabled"] = enabled

        if not values:
            return await self.get(agent_id)

        async with self._sf() as db:
            result = await db.execute(
                update(WebsiteAgent)
                .where(WebsiteAgent.id == agent_id)
                .values(**values)
                .returning(WebsiteAgent)
            )
            row = result.scalar_one_or_none()
            await db.commit()

        if row is None:
            return None

        # Evict caches unconditionally -- cheap, and the callers who
        # care (disable, origin shrink) need it.
        _ID_CACHE.invalidate(str(agent_id))
        _KEY_CACHE.invalidate(row.publishable_key_hash)
        return _row_to_resolved(row)

    async def delete(self, agent_id: UUID) -> bool:
        """Hard-delete a website agent.

        Returns True when the row existed and was removed.  Peer
        processes converge on the disappearance within the cache TTL;
        the calling process evicts immediately.
        """
        async with self._sf() as db:
            row = await db.get(WebsiteAgent, agent_id)
            if row is None:
                return False
            key_hash = row.publishable_key_hash
            await db.delete(row)
            await db.commit()
        _ID_CACHE.invalidate(str(agent_id))
        _KEY_CACHE.invalidate(key_hash)
        return True

    # ------------------------------------------------------------------
    # Reads (bootstrap-facing)
    # ------------------------------------------------------------------

    async def get(self, agent_id: UUID) -> ResolvedWebsiteAgent | None:
        """Fetch the full resolved projection for *agent_id*.

        Consulted by the session-cookie decode path so a running session
        can be validated against the current row (caught mid-flight if
        ops disables the agent).  Cache TTL applies.
        """
        cached = _ID_CACHE.get(str(agent_id))
        if cached is not None:
            return cached

        async with self._sf() as db:
            row = await db.get(WebsiteAgent, agent_id)
        if row is None:
            return None
        resolved = _row_to_resolved(row)
        _ID_CACHE.set(str(agent_id), resolved)
        _KEY_CACHE.set(row.publishable_key_hash, resolved)
        return resolved

    async def get_by_publishable_key(
        self, token: str
    ) -> ResolvedWebsiteAgent | None:
        """Resolve a raw publishable key to a :class:`ResolvedWebsiteAgent`.

        Returns None for unknown or disabled rows.  The bootstrap route
        additionally checks the request's ``Origin`` against
        :attr:`ResolvedWebsiteAgent.allowed_origins`; authority is the
        conjunction of (valid key) AND (origin in allow-list).
        """
        key_hash = hash_publishable_key(token)
        cached = _KEY_CACHE.get(key_hash)
        if cached is not None:
            return cached

        async with self._sf() as db:
            result = await db.execute(
                select(WebsiteAgent).where(
                    WebsiteAgent.publishable_key_hash == key_hash,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        resolved = _row_to_resolved(row)
        _KEY_CACHE.set(key_hash, resolved)
        _ID_CACHE.set(str(resolved.id), resolved)
        return resolved

    async def list_for_org(self, org_id: UUID) -> list[WebsiteAgent]:
        """Return all website agents belonging to *org_id*, newest first.

        Surfaces the full ORM row (not the resolved projection) so ops
        tooling can render admin-oriented fields like the display
        prefix and creation timestamp.
        """
        async with self._sf() as db:
            result = await db.execute(
                select(WebsiteAgent)
                .where(WebsiteAgent.org_id == org_id)
                .order_by(WebsiteAgent.created_at.desc())
            )
            return list(result.scalars().all())


def origin_allowed(origin: str | None, allowed: tuple[str, ...]) -> bool:
    """Return True when *origin* is present in *allowed* after normalisation.

    Exact match only -- wildcards and subdomain matching are deliberately
    out of scope for the public-website surface.  Ops should explicitly
    list every origin that may embed an agent.
    """
    if not origin:
        return False
    return _normalize_origin(origin) in allowed
