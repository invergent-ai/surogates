"""Redis-backed non-waking observation queue for followed channels.

Channel adapters run outside the harness worker, so they cannot call the
in-process memory provider directly.  They append firehose observations
to this bounded Redis list; the worker drains the list into channel memory
at the next wake before prefetch.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "append_channel_observation",
    "channel_observation_key",
    "drain_channel_observations",
]


def channel_observation_key(agent_id: str, channel_id: str) -> str:
    return f"mate:channel-observations:{agent_id}:{channel_id}"


async def append_channel_observation(
    redis: Any,
    *,
    agent_id: str,
    channel_id: str,
    observation: dict[str, Any],
    maxlen: int = 1000,
) -> None:
    key = channel_observation_key(agent_id, channel_id)
    payload = json.dumps(observation, ensure_ascii=False, sort_keys=True)
    await redis.rpush(key, payload)
    await redis.ltrim(key, -maxlen, -1)


async def drain_channel_observations(
    redis: Any,
    *,
    agent_id: str,
    channel_id: str,
    max_items: int = 1000,
) -> list[dict[str, Any]]:
    key = channel_observation_key(agent_id, channel_id)
    raw = await redis.lpop(key, max_items)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]

    observations: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, bytes):
            item = item.decode("utf-8")
        try:
            parsed = json.loads(str(item))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            observations.append(parsed)
    return observations
