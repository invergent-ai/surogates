"""The ``fetch_channel_file`` builtin tool.

Lets a Slack-channel agent pull a file shared in an earlier message of its
own channel (the id comes from the backfilled history, shown as
``name (file: F…)``).  Thin delegate to the session-scoped harness API
client; the privileged download/ingest runs server-side.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

FETCH_CHANNEL_FILE_SCHEMA = ToolSchema(
    name="fetch_channel_file",
    description=(
        "Fetch a file that was shared earlier in this channel and load it "
        "into your workspace. Pass the Slack file id shown in the channel "
        "history as 'name (file: F…)'. The file is downloaded under "
        "uploads/slack/fetch/ and, when textual, its content is returned "
        "inline. Only files shared in this channel can be fetched."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": (
                    "The Slack file id from the channel history, e.g. "
                    "'F0BE46MG31P'."
                ),
            },
        },
        "required": ["file_id"],
    },
)


async def _fetch_channel_file_handler(
    arguments: dict[str, Any], **kwargs: Any,
) -> str:
    file_id = (arguments.get("file_id") or "").strip()
    if not file_id:
        return json.dumps(
            {"success": False, "error": "A file_id is required."},
            ensure_ascii=False,
        )
    api_client = kwargs.get("api_client")
    if api_client is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Channel-file fetch requires a session-scoped API client."
                ),
            },
            ensure_ascii=False,
        )
    return await api_client.fetch_channel_file(file_id)


def register(registry: ToolRegistry) -> None:
    """Register the fetch_channel_file tool."""
    registry.register(
        name="fetch_channel_file",
        schema=FETCH_CHANNEL_FILE_SCHEMA,
        handler=_fetch_channel_file_handler,
        toolset="channels",
    )
