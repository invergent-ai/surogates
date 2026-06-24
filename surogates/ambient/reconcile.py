"""Reconcile the ambient schedule for a channel from its resolved settings.

Called when a followed channel session wakes: ensure (idempotent) an active
ambient schedule when Ambient is enabled, or deactivate it when disabled.  The
per-channel caps travel on the schedule config so the materializer can copy
them onto the ambient session, where the gated post tool reads them.
"""

from __future__ import annotations

from typing import Any

_CAP_FIELDS = (
    "confidence_threshold",
    "max_proactive_posts_per_day",
    "min_seconds_between_posts",
    "quiet_thread_minutes",
)


async def reconcile_ambient_schedule(
    ambient_store: Any,
    *,
    settings_dict: dict[str, Any],
    org_id: Any,
    agent_id: str,
    platform: str,
    channel_id: str,
    source_session_id: Any,
    team_id: str,
) -> None:
    if settings_dict.get("ambient_enabled"):
        caps = {f: settings_dict[f] for f in _CAP_FIELDS if f in settings_dict}
        await ambient_store.ensure(
            org_id=org_id,
            agent_id=agent_id,
            platform=platform,
            channel_id=channel_id,
            source_session_id=source_session_id,
            cadence_seconds=int(settings_dict.get("ambient_cadence_seconds", 1800)),
            config={"slack_team_id": team_id, "ambient_caps": caps},
        )
    else:
        existing = await ambient_store.get(
            agent_id=agent_id, platform=platform, channel_id=channel_id,
        )
        if existing is not None:
            await ambient_store.deactivate(existing.id)
