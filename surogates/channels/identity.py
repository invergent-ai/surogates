"""Shared identity resolution for channel adapters.

Provides the two operations every adapter needs:
1. Resolve a platform user ID to a Surogates user (via channel_identities table).
2. Get or create a Surogates session for the resolved user + channel routing key.

Used by Slack, Teams, Telegram, and Webhook adapters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.channels.memory_boundary import MANAGED_CHANNELS
from surogates.db.models import ChannelIdentity as ChannelIdentityRow
from surogates.db.models import Session as SessionRow
from surogates.session.events import EventType
from surogates.session.provisioning import (
    pin_workspace_boundary,
    stamp_workspace_config,
)
from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)


@dataclass
class ResolvedIdentity:
    """Result of resolving a platform user to a Surogates identity."""

    user_id: UUID
    org_id: UUID
    platform: str
    platform_user_id: str


async def resolve_identity(
    session_factory: async_sessionmaker[AsyncSession],
    platform: str,
    platform_user_id: str,
    *,
    org_id: UUID | None = None,
) -> ResolvedIdentity | None:
    """Look up a channel identity and its associated user in the database.

    When ``org_id`` is given the lookup is scoped to that org, so the same
    platform user id resolves independently per tenant (a Slack workspace
    member known to two different orgs' agents gets each org's own identity).
    Returns a ``ResolvedIdentity`` or ``None`` if the platform user is not
    registered for the org.
    """
    from surogates.db.models import User as UserRow

    async with session_factory() as db:
        query = (
            select(ChannelIdentityRow, UserRow)
            .join(UserRow, ChannelIdentityRow.user_id == UserRow.id)
            .where(ChannelIdentityRow.platform == platform)
            .where(ChannelIdentityRow.platform_user_id == platform_user_id)
        )
        if org_id is not None:
            query = query.where(ChannelIdentityRow.org_id == org_id)
        row = (await db.execute(query)).first()
        if row is None:
            return None
        identity, user = row
        return ResolvedIdentity(
            user_id=user.id,
            org_id=user.org_id,
            platform=identity.platform,
            platform_user_id=identity.platform_user_id,
        )


async def resolve_real_identity(
    session_factory: async_sessionmaker[AsyncSession],
    platform: str,
    platform_user_id: str,
    *,
    org_id: object,
) -> ResolvedIdentity | None:
    """Resolve a channel sender to a LINKED real user, or ``None``.

    Like :func:`resolve_identity` but filtered to **real** (non-shadow) users —
    a ``shadow`` user (``auth_provider == platform``, created by auto-provision)
    does not count as linked.  Never provisions; ``linked`` mode uses this to
    decide between "known real user" and "prompt to link".
    """
    from surogates.db.models import User as UserRow

    async with session_factory() as db:
        row = (
            await db.execute(
                select(ChannelIdentityRow, UserRow)
                .join(UserRow, ChannelIdentityRow.user_id == UserRow.id)
                .where(ChannelIdentityRow.platform == platform)
                .where(ChannelIdentityRow.platform_user_id == platform_user_id)
                .where(ChannelIdentityRow.org_id == org_id)
                .where(UserRow.auth_provider != platform)
            )
        ).first()
        if row is None:
            return None
        identity, user = row
        return ResolvedIdentity(
            user_id=user.id,
            org_id=user.org_id,
            platform=identity.platform,
            platform_user_id=identity.platform_user_id,
        )


async def get_or_create_channel_identity(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    platform_user_id: str,
    org_id: UUID,
    display_name: str = "",
) -> ResolvedIdentity:
    """Resolve a channel participant to an identity, provisioning if unknown.

    Channel access is authorised by membership (an admin installed the app and
    invited the agent), not by a Surogate platform account — so a sender who is
    not yet linked must NOT be turned away.  On first contact we get-or-create a
    lightweight external ``User`` (``auth_provider=<platform>``,
    ``external_id=<platform_user_id>``) scoped to the agent's ``org_id`` and
    link an org-scoped ``channel_identities`` row to it, then return the
    resolved identity.

    Both inserts use ``ON CONFLICT DO NOTHING`` against their unique indexes
    (``uq_users_org_auth_external`` / ``uq_channel_org_platform``), so the
    function is idempotent and safe under concurrent first-contact messages
    without a manual rollback/retry dance — the trailing re-resolve returns the
    row that won.

    An existing identity (including one a user explicitly linked to their real
    platform account) always wins — provisioning only runs when no identity
    exists for the org yet.
    """
    from surogates.db.models import User as UserRow

    # routing.org_id arrives as a str; normalise so the returned identity's
    # org_id is a UUID on every path (cached, resolved, or freshly provisioned).
    if not isinstance(org_id, UUID):
        org_id = UUID(str(org_id))

    existing = await resolve_identity(
        session_factory, platform, platform_user_id, org_id=org_id
    )
    if existing is not None:
        return existing

    # Synthetic address namespaced by org so the same platform user id in two
    # orgs (or two ids differing only by a stripped '@') can't collide; there
    # is no unique constraint on email, but downstream lookups treat it as a key.
    local_part = platform_user_id.lstrip("@")
    email = f"{platform}-{local_part}@{org_id}.channels.surogate.local"

    async with session_factory() as db:
        user_id = (
            await db.execute(
                pg_insert(UserRow)
                .values(
                    org_id=org_id,
                    email=email,
                    display_name=display_name or platform_user_id,
                    auth_provider=platform,
                    external_id=platform_user_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["org_id", "auth_provider", "external_id"],
                    index_where=UserRow.external_id.isnot(None),
                )
                .returning(UserRow.id)
            )
        ).scalar_one_or_none()
        if user_id is None:
            # The user already existed (concurrent insert or a prior link) —
            # fetch it to attach the identity.
            user_id = (
                await db.execute(
                    select(UserRow.id)
                    .where(UserRow.org_id == org_id)
                    .where(UserRow.auth_provider == platform)
                    .where(UserRow.external_id == platform_user_id)
                )
            ).scalar_one()

        await db.execute(
            pg_insert(ChannelIdentityRow)
            .values(
                org_id=org_id,
                user_id=user_id,
                platform=platform,
                platform_user_id=platform_user_id,
            )
            .on_conflict_do_nothing(
                index_elements=["org_id", "platform", "platform_user_id"],
            )
        )
        await db.commit()

    logger.info(
        "Provisioned %s channel identity for %s (org %s)",
        platform, platform_user_id, org_id,
    )
    # Every field is already in hand — user_id from the insert's RETURNING (or
    # the conflict re-SELECT), the rest are arguments — so build the result
    # directly rather than a third round-trip back to the DB.
    return ResolvedIdentity(
        user_id=user_id,
        org_id=org_id,
        platform=platform,
        platform_user_id=platform_user_id,
    )


def make_cached_identity_resolver(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    resolve=resolve_identity,
    provision=get_or_create_channel_identity,
    ttl_seconds: float = 30.0,
    max_entries: int = 10_000,
):
    """Return a caching wrapper around :func:`get_or_create_channel_identity`.

    The inbound pipeline resolves a sender's identity on every message; for an
    already-known sender that is an immutable DB read.  This memoizes it per
    ``(platform, platform_user_id, org)`` for ``ttl_seconds`` — reusing
    :class:`~surogates.runtime.channel_routing_cache.ChannelRoutingCache`'s TTL,
    single-flight and negative-cache — so a chatty sender doesn't re-hit the DB
    each message.  The short TTL bounds staleness if an identity is ever
    re-linked (e.g. a future explicit account-link flow); ``max_entries`` bounds
    memory since the sender keyspace is unbounded (unlike the routing cache).

    ``resolve`` / ``provision`` are injectable for testing; production uses the
    module-level functions.
    """
    from surogates.runtime.channel_routing_cache import ChannelRoutingCache

    def _key(platform: str, platform_user_id: str, org_id: object) -> str:
        # NUL separates the parts so a platform id containing the separator
        # can't forge a collision.
        return f"{platform}\x00{platform_user_id}\x00{org_id}"

    async def _loader(key: str) -> ResolvedIdentity | None:
        platform, platform_user_id, org_id = key.split("\x00")
        # ChannelRoutingCache stores the loader's value verbatim, so cache the
        # ResolvedIdentity object directly — no dict round-trip.
        return await resolve(
            session_factory, platform, platform_user_id, org_id=org_id
        )

    cache = ChannelRoutingCache(
        loader=_loader, ttl_seconds=ttl_seconds, max_entries=max_entries
    )

    async def _resolver(
        sf: async_sessionmaker[AsyncSession],
        platform: str,
        platform_user_id: str,
        *,
        org_id: object,
        display_name: str = "",
    ) -> ResolvedIdentity | None:
        # ``sf`` is the session_factory the pipeline forwards; it is always the
        # one captured above (which the loader uses), so we use the captured
        # factory throughout for a single source of truth.
        key = _key(platform, platform_user_id, org_id)
        cached = await cache.get(key)
        if cached is not None or provision is None:
            # ``provision is None`` (linked policy): resolve-only — return the
            # real identity or None (the caller prompts the sender to link).
            return cached
        # Unknown sender (shadow policy): provision, then seed the cache so this
        # sender's next message is a hit, not a reload.
        ident = await provision(
            session_factory,
            platform=platform,
            platform_user_id=platform_user_id,
            org_id=org_id,
            display_name=display_name,
        )
        cache.set(key, ident)
        return ident

    # Expose the cache so the channels process can wire it into the
    # cross-process invalidator: link_channel evicts a just-linked sender's
    # negative-cache entry on bind instead of waiting out ``ttl_seconds``.
    _resolver.cache = cache
    return _resolver


# Statuses whose session is reused for a routing key. completed/paused are
# idle-but-resumable (re-activated on reuse); active/processing are in flight.
# Any other status (failed, archived, …) starts a fresh session.
_RESUMABLE_STATUSES = ("active", "processing", "paused", "completed")


async def get_or_create_channel_session(
    session_store: SessionStore,
    redis: object,
    *,
    session_key: str,
    user_id: UUID,
    org_id: UUID,
    agent_id: str,
    channel: str,
    config: dict,
    session_factory: async_sessionmaker[AsyncSession],
    model: str = "",
    storage: object = None,
    settings: object = None,
) -> UUID:
    """Reuse the most-recent session for the channel routing key — resuming an
    idle (completed/paused) one so the conversation continues — or create a new
    session. The caller (the inbound pipeline) enqueues the returned session.

    Parameters
    ----------
    session_store:
        The Surogates session store (PostgreSQL-backed).
    redis:
        The async Redis client for enqueuing to the work queue.
    session_key:
        Deterministic routing key from ``build_session_key()``.
    user_id, org_id, agent_id:
        The resolved Surogates user, org, and the agent this session
        belongs to.
    channel:
        Channel name (e.g. ``"slack"``, ``"teams"``).
    config:
        Channel-specific session config (e.g. Slack channel_id, thread_ts).
    session_factory:
        SQLAlchemy async session factory for direct DB queries.
    model:
        LLM model override for this channel (empty = use global default).

    Returns
    -------
    UUID
        The session ID (existing or newly created).
    """
    # Always-resume: fetch the MOST-RECENT session for this routing key,
    # regardless of status (order-then-decide). Deciding in Python — rather than
    # filtering statuses in SQL — ensures a failed/terminal *most-recent* session
    # starts fresh below instead of resurrecting an older completed one behind it.
    async with session_factory() as db:
        result = await db.execute(
            select(SessionRow)
            .where(SessionRow.user_id == user_id)
            .where(SessionRow.agent_id == agent_id)
            .where(SessionRow.channel == channel)
            .where(
                SessionRow.config["channel_session_key"].as_string() == session_key
            )
            .order_by(SessionRow.created_at.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        existing_id = existing.id if existing else None
        existing_status = existing.status if existing else None
    # Reuse the session when it is in a resumable state. An idle one
    # (completed/paused) is re-activated + resume-tagged so the worker continues
    # it (the harness then replays the full prior conversation); active/processing
    # are reused as-is. Anything else (failed, archived, …) — including as the
    # most recent — falls through to a fresh session.
    if existing_id is not None and existing_status in _RESUMABLE_STATUSES:
        # Backfill boundary partitioning onto sessions created before the
        # thread's boundary was known (or before workspace partitioning
        # existed). Managed-channel threads carry a live ``memory_boundary``
        # on every inbound message; mirror it onto the session's memory and
        # workspace boundaries so a resumed session joins the shared
        # partition instead of stranding attachments in its own prefix.
        incoming_boundary = str(config.get("memory_boundary") or "").strip()
        if channel in MANAGED_CHANNELS and incoming_boundary:
            existing_config = getattr(existing, "config", None) or {}
            if existing_config.get("memory_boundary") != incoming_boundary:
                await session_store.update_session_config_key(
                    existing_id, "memory_boundary", incoming_boundary,
                )
            if existing_config.get("workspace_boundary") != incoming_boundary:
                await session_store.update_session_config_key(
                    existing_id, "workspace_boundary", incoming_boundary,
                )
        if existing_status in ("completed", "paused"):
            await session_store.resume_session(existing_id, source="channel_message")
            logger.info(
                "Resuming %s session %s (status=%s, key=%s)",
                channel, existing_id, existing_status, session_key,
            )
        return existing_id

    # Create a new session.
    session_id = uuid4()
    merged_config = {
        "channel_session_key": session_key,
        **config,
    }
    # Pin the managed-channel thread's memory boundary as the workspace
    # boundary so this new session shares the thread's partitioned workspace.
    pin_workspace_boundary(merged_config, channel=channel)
    # Provision a persistent workspace (storage_bucket/storage_key_prefix/
    # workspace_path) the same way API/web sessions do, so the worker mounts a
    # persistent /workspace and inbound attachments can be written there. A
    # storage failure here must NOT abort session creation — that would 500 the
    # inbound webhook and trigger platform retries — so degrade to a
    # workspace-less session and log instead.
    if storage is not None and settings is not None:
        try:
            await stamp_workspace_config(
                merged_config,
                storage=storage,
                settings=settings,
                session_id=session_id,
                model=model,
            )
        except Exception:
            logger.warning(
                "Failed to provision workspace for %s session %s; "
                "creating it without one",
                channel,
                session_id,
                exc_info=True,
            )
    await session_store.create_session(
        session_id=session_id,
        user_id=user_id,
        org_id=org_id,
        agent_id=agent_id,
        channel=channel,
        model=model,
        config=merged_config,
    )

    logger.info(
        "Created %s session %s for user %s (key: %s)",
        channel, session_id, user_id, session_key,
    )

    return session_id
