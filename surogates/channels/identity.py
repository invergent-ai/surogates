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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import ChannelIdentity as ChannelIdentityRow
from surogates.db.models import Session as SessionRow
from surogates.session.events import EventType
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
) -> ResolvedIdentity | None:
    """Look up a channel identity and its associated user in the database.

    Returns a ``ResolvedIdentity`` with ``user_id`` and ``org_id``, or
    ``None`` if the platform user is not registered.
    """
    from surogates.db.models import User as UserRow

    async with session_factory() as db:
        result = await db.execute(
            select(ChannelIdentityRow, UserRow)
            .join(UserRow, ChannelIdentityRow.user_id == UserRow.id)
            .where(ChannelIdentityRow.platform == platform)
            .where(ChannelIdentityRow.platform_user_id == platform_user_id)
        )
        row = result.first()
        if row is None:
            return None
        identity, user = row
        return ResolvedIdentity(
            user_id=user.id,
            org_id=user.org_id,
            platform=identity.platform,
            platform_user_id=identity.platform_user_id,
        )


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
) -> UUID:
    """Find an existing active session for the channel routing key, or create one.

    Also enqueues the session to the Redis work queue so the worker picks
    it up.

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
        belongs to (``Settings.agent_id``).
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
    # Check database for existing active session with this routing key.
    async with session_factory() as db:
        result = await db.execute(
            select(SessionRow)
            .where(SessionRow.user_id == user_id)
            .where(SessionRow.agent_id == agent_id)
            .where(SessionRow.channel == channel)
            .where(SessionRow.status.in_(["active", "processing", "paused"]))
            .where(
                SessionRow.config["channel_session_key"].as_string() == session_key
            )
            .order_by(SessionRow.created_at.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing.id

    # Create a new session.
    session_id = uuid4()
    merged_config = {
        "channel_session_key": session_key,
        **config,
    }
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
