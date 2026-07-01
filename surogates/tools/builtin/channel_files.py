"""The ``fetch_channel_file`` builtin tool.

Lets a Slack-channel agent pull a file shared anywhere in its own channel
(including threads and messages from other users) by name or id.  The name
comes from the channel history (``name (file: F…)`` format), and can be passed
as-is or as the bare filename.  Thin delegate to the session-scoped harness API
client; the privileged resolution, download and ingest runs server-side.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

FETCH_CHANNEL_FILE_SCHEMA = ToolSchema(
    name="fetch_channel_file",
    description=(
        "Fetch a file shared anywhere in this channel (including threads and "
        "messages from other users) and load it into your workspace. Pass the "
        "file's name as shown in the channel (e.g. 'report.html') or its Slack "
        "file id (e.g. 'F0BE46MG31P'). The file is downloaded under "
        "uploads/slack/fetch/ and, when textual, its content is returned "
        "inline. Only files shared in this channel can be fetched."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": (
                    "The file to fetch: either its name as shown in the channel "
                    "(e.g. 'report.html') or its Slack file id (e.g. 'F0BE46MG31P')."
                ),
            },
        },
        "required": ["file"],
    },
)


async def _fetch_channel_file_handler(
    arguments: dict[str, Any], **kwargs: Any,
) -> str:
    ref = (arguments.get("file") or arguments.get("file_id") or "").strip()
    if not ref:
        return json.dumps(
            {"success": False, "error": "A file name or file id is required."},
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
    return await api_client.fetch_channel_file(ref)


def register(registry: ToolRegistry) -> None:
    """Register the fetch_channel_file tool."""
    registry.register(
        name="fetch_channel_file",
        schema=FETCH_CHANNEL_FILE_SCHEMA,
        handler=_fetch_channel_file_handler,
        toolset="channels",
    )
