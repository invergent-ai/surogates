"""Service-account token generation, verification, and CRUD.

Service accounts are org-scoped API keys used by non-interactive clients
(synthetic data pipelines, batch jobs) to submit prompts over the REST
API.  Unlike JWTs they carry no user identity and are long-lived until
revoked.

Token format:

    surg_sk_<44 base64url characters>

The raw token is returned to the caller exactly once on creation.  Only
a SHA-256 hex digest is persisted, enabling constant-time lookups
without storing the secret.  The ``surg_sk_`` prefix is how the auth
middleware distinguishes service-account tokens from JWTs before
attempting to decode.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import ServiceAccount

logger = logging.getLogger(__name__)

__all__ = [
    "TOKEN_PREFIX",
    "IssuedServiceAccount",
    "ResolvedServiceAccount",
    "ServiceAccountStore",
    "generate_token",
    "hash_token",
    "is_service_account_token",
]


TOKEN_PREFIX = "surg_sk_"
_DISPLAY_PREFIX_LEN = len(TOKEN_PREFIX) + 8
# 33 raw bytes -> 44 chars of base64url (no padding).  264 bits of entropy.
_SECRET_BYTES = 33
_CACHE_TTL_SECONDS: float = 60.0


@dataclass(frozen=True, slots=True)
class ResolvedServiceAccount:
    """Fields the auth middleware needs after a token resolution.

    Returning this lightweight view — instead of the SQLAlchemy
    ``ServiceAccount`` ORM row — lets a cache hit skip the DB entirely.
    """

    id: UUID
    org_id: UUID


class _TTLCache:
    """Tiny TTL cache for resolved service-account identities.

    Process-local; every API/worker replica has its own.  A cache hit
    returns the stored :class:`ResolvedServiceAccount` without opening
    a DB session — that is the performance win.  Revocation in another
    process is bounded by the TTL (the cached process keeps accepting
    the token until the entry expires); revocation in the same process
    is applied instantly via :meth:`invalidate`.
    """

    __slots__ = ("_entries", "_ttl")

    def __init__(self, ttl_seconds: float) -> None:
        # key -> (expires_monotonic, ResolvedServiceAccount)
        self._entries: dict[str, tuple[float, ResolvedServiceAccount]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> ResolvedServiceAccount | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._entries.pop(key, None)
            return None
        return value

    def set(self, key: str, value: ResolvedServiceAccount) -> None:
        self._entries[key] = (time.monotonic() + self._ttl, value)

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


# Module-level singletons so two different callers in the same process
# (e.g. auth middleware and a follow-up admin call) see the same
# revocation state.
_ROW_CACHE_BY_HASH: _TTLCache = _TTLCache(_CACHE_TTL_SECONDS)
_ROW_CACHE_BY_ID: _TTLCache = _TTLCache(_CACHE_TTL_SECONDS)


def _reset_caches() -> None:
    """Clear the SA auth caches.  Exposed for tests."""
    _ROW_CACHE_BY_HASH.clear()
    _ROW_CACHE_BY_ID.clear()


def is_service_account_token(token: str) -> bool:
    """Return True when *token* carries the service-account prefix."""
    return token.startswith(TOKEN_PREFIX)


def generate_token() -> str:
    """Return a freshly minted, high-entropy service-account token."""
    return TOKEN_PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of *token* for storage and lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedServiceAccount:
    """A service account freshly created by :meth:`ServiceAccountStore.create`.

    The raw *token* is present here and nowhere else — callers must
    surface it to the end user immediately; it cannot be recovered
    later.
    """

    id: UUID
    org_id: UUID
    name: str
    token: str
    token_prefix: str
    created_at: datetime


class ServiceAccountStore:
    """CRUD for ``service_accounts``.

    The store is stateless; all persistence happens through the supplied
    ``async_sessionmaker``.  Token secrets are hashed here before they
    touch the database.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(self, *, org_id: UUID, name: str) -> IssuedServiceAccount:
        """Issue a new service account and return its raw token exactly once."""
        token = generate_token()
        row = ServiceAccount(
            org_id=org_id,
            name=name,
            token_hash=hash_token(token),
            token_prefix=token[:_DISPLAY_PREFIX_LEN],
        )
        async with self._sf() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)

        return IssuedServiceAccount(
            id=row.id,
            org_id=row.org_id,
            name=row.name,
            token=token,
            token_prefix=row.token_prefix,
            created_at=row.created_at,
        )

    async def get_by_token(self, token: str) -> ResolvedServiceAccount | None:
        """Resolve a raw bearer token to a :class:`ResolvedServiceAccount`.

        Returns ``None`` when the token is unknown or revoked.  A
        successful lookup populates the process-local caches for
        :data:`_CACHE_TTL_SECONDS`; subsequent requests return the
        cached value without opening a DB session at all.
        ``last_used_at`` is bumped only on cache miss, so a high-RPS
        harness workload does not produce a per-request write hotspot
        on one row.

        Revocation via :meth:`revoke` invalidates the cache entry
        immediately in the process that performed the revoke.  Peer
        processes converge on the revocation within
        :data:`_CACHE_TTL_SECONDS` — during that window a cached
        process may still accept the token.  This is the explicit
        trade-off behind the cache.
        """
        token_hash = hash_token(token)

        cached = _ROW_CACHE_BY_HASH.get(token_hash)
        if cached is not None:
            return cached

        async with self._sf() as db:
            result = await db.execute(
                select(ServiceAccount).where(
                    ServiceAccount.token_hash == token_hash,
                    ServiceAccount.revoked_at.is_(None),
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None

            resolved = ResolvedServiceAccount(id=row.id, org_id=row.org_id)

            # Best-effort heartbeat; failures here must not deny auth.
            try:
                await db.execute(
                    update(ServiceAccount)
                    .where(ServiceAccount.id == row.id)
                    .values(last_used_at=func.now())
                )
                await db.commit()
            except Exception:
                logger.debug(
                    "Failed to update last_used_at for service account %s",
                    row.id,
                    exc_info=True,
                )

        _ROW_CACHE_BY_HASH.set(token_hash, resolved)
        _ROW_CACHE_BY_ID.set(str(resolved.id), resolved)
        return resolved

    async def get_by_id(
        self, service_account_id: UUID, org_id: UUID
    ) -> ResolvedServiceAccount | None:
        """Resolve ``service_account_id`` → :class:`ResolvedServiceAccount`.

        Used by the ``service_account_session`` JWT path: the token
        already names the service account, but we still verify that
        the row exists, belongs to the claimed org, and is not revoked.
        Same cache semantics as :meth:`get_by_token` — revoking the
        account invalidates both entries.  Same peer-process lag.
        """
        cache_key = str(service_account_id)
        cached = _ROW_CACHE_BY_ID.get(cache_key)
        if cached is not None:
            if cached.org_id != org_id:
                logger.warning(
                    "Service account %s presented with mismatched org_id",
                    service_account_id,
                )
                return None
            return cached

        async with self._sf() as db:
            result = await db.execute(
                select(ServiceAccount).where(
                    ServiceAccount.id == service_account_id,
                    ServiceAccount.org_id == org_id,
                    ServiceAccount.revoked_at.is_(None),
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        resolved = ResolvedServiceAccount(id=row.id, org_id=row.org_id)
        _ROW_CACHE_BY_ID.set(cache_key, resolved)
        _ROW_CACHE_BY_HASH.set(row.token_hash, resolved)
        return resolved

    async def list_for_org(self, org_id: UUID) -> list[ServiceAccount]:
        """Return all service accounts belonging to *org_id*, newest first."""
        async with self._sf() as db:
            result = await db.execute(
                select(ServiceAccount)
                .where(ServiceAccount.org_id == org_id)
                .order_by(ServiceAccount.created_at.desc())
            )
            return list(result.scalars().all())

    async def revoke(self, *, service_account_id: UUID, org_id: UUID) -> bool:
        """Mark a service account as revoked.

        Returns True when the row was found and updated, False when it
        does not exist, belongs to another org, or was already revoked.
        Uses the database clock to match the naive-timestamp convention
        of the ``service_accounts`` columns.

        Invalidates the in-process auth caches so outstanding tokens
        stop resolving in this process on the next request.  Peer
        processes converge within ``_CACHE_TTL_SECONDS``.
        """
        async with self._sf() as db:
            result = await db.execute(
                update(ServiceAccount)
                .where(
                    ServiceAccount.id == service_account_id,
                    ServiceAccount.org_id == org_id,
                    ServiceAccount.revoked_at.is_(None),
                )
                .values(revoked_at=func.now())
                .returning(ServiceAccount.token_hash)
            )
            token_hash = result.scalar_one_or_none()
            await db.commit()

        if token_hash is None:
            return False

        _ROW_CACHE_BY_ID.invalidate(str(service_account_id))
        _ROW_CACHE_BY_HASH.invalidate(token_hash)
        return True
