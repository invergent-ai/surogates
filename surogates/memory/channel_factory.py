"""Factory that builds a loaded ChannelMemoryProvider for a session.

Keeps the per-session wiring out of the worker hot path and unit-testable
in isolation.  Returns None for sessions that are not follow-enabled channel
sessions, so the worker can register conditionally.  When a provider is
built, queued Redis observations for that ``agent_id + channel_id`` are
drained into it before return so they are recallable on this wake.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.channels.channel_follow import channel_follow_enabled
from surogates.channels.channel_observations import drain_channel_observations
from surogates.memory.channel_provider import ChannelMemoryProvider
from surogates.memory.channel_store import ChannelMemoryStore

logger = logging.getLogger(__name__)


def _channel_id_for(session: Any) -> str | None:
    config = getattr(session, "config", None) or {}
    return config.get("slack_channel_id")


async def build_channel_provider(
    session: Any,
    *,
    storage_backend: Any,
    bucket: str,
    redis_client: Any,
) -> ChannelMemoryProvider | None:
    """Return a loaded ChannelMemoryProvider, or None if not applicable."""
    if not channel_follow_enabled(session):
        return None
    channel_id = _channel_id_for(session)
    if not channel_id:
        return None
    org_id = getattr(session, "org_id", None) or "shared"
    agent_id = getattr(session, "agent_id", None) or "shared"
    key = f"channel-memory/{org_id}/{agent_id}/{channel_id}"
    store = ChannelMemoryStore(backend=storage_backend, bucket=bucket, key=key)
    try:
        await store.load()
    except Exception:
        logger.warning("Channel memory load failed for %s; starting empty", key, exc_info=True)
    provider = ChannelMemoryProvider(store, channel_id=channel_id)
    try:
        observations = await drain_channel_observations(
            redis_client, agent_id=str(agent_id), channel_id=channel_id,
        )
    except Exception:
        logger.warning("Channel observation drain failed for %s", key, exc_info=True)
        observations = []
    for observation in observations:
        source = observation.get("source") or {}
        provider.ingest(
            str(observation.get("content") or ""),
            meta={
                "user_name": source.get("user_name"),
                "user": source.get("user_id"),
                "ts": observation.get("ts", ""),
            },
        )
    return provider
