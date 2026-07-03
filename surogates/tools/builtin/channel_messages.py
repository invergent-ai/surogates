"""The ``fetch_channel_messages`` builtin tool.

Lets a Slack-channel agent read recent messages posted in its own channel,
optionally narrowed by a time window or a specific user. Thin delegate to the
session-scoped harness API client; the privileged history fetch runs
server-side with the channel's bot token.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

FETCH_CHANNEL_MESSAGES_SCHEMA = ToolSchema(
    name="fetch_channel_messages",
    description=(
        "Read recent messages posted in this Slack channel (including messages "
        "from other users). Use this to catch up on the conversation or to see "
        "what a specific person said. Optionally narrow by 'since' (e.g. '24h', "
        "'7d', or a date like '2026-07-01') and by 'user' (a Slack user id such "
        "as 'U063C2DB7GW' or a mention like '<@U063C2DB7GW>'). Returns messages "
        "oldest-to-newest. Only messages in this channel are accessible."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many recent messages to return (default 50, max 200).",
            },
            "since": {
                "type": "string",
                "description": "Only messages newer than this: '24h', '7d', or a date '2026-07-01'.",
            },
            "user": {
                "type": "string",
                "description": "Only messages from this Slack user id or mention (e.g. '<@U063C2DB7GW>').",
            },
        },
        "required": [],
    },
)


def _coerce_limit(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _fetch_channel_messages_handler(
    arguments: dict[str, Any], **kwargs: Any,
) -> str:
    api_client = kwargs.get("api_client")
    if api_client is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Channel-message fetch requires a session-scoped API client."
                ),
            },
            ensure_ascii=False,
        )
    return await api_client.fetch_channel_messages(
        limit=_coerce_limit(arguments.get("limit")),
        since=(arguments.get("since") or None),
        user=(arguments.get("user") or None),
    )


def register(registry: ToolRegistry) -> None:
    """Register the fetch_channel_messages tool."""
    registry.register(
        name="fetch_channel_messages",
        schema=FETCH_CHANNEL_MESSAGES_SCHEMA,
        handler=_fetch_channel_messages_handler,
        toolset="channels",
    )
