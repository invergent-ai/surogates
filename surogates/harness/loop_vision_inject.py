"""Vision-block injection for fetched channel images.

Pure helper: given tool results + tool calls (to match names), and an async
``read_image(path) -> (bytes, mime) | None`` callback, returns the extra
user messages (vision blocks) to append after the tool results for
vision-capable models.  No loop state required.

Called from ``loop.py`` between ``execute_tool_calls`` and
``enforce_turn_budget``.  All failures are best-effort: any exception
during storage read or JSON parsing returns [] and never breaks the turn.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Raster mimes eligible for native vision injection.
_RASTER_IMAGE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
})


def _is_raster_mime(mime: str) -> bool:
    return (mime or "").lower().split(";")[0].strip() in _RASTER_IMAGE_MIMES


def _tool_name_for_call_id(tool_calls_raw: list[dict], call_id: str) -> str:
    """Return the tool name for *call_id*, or '' if not found."""
    for tc in tool_calls_raw:
        if tc.get("id") == call_id:
            return (tc.get("function") or {}).get("name") or ""
    return ""


async def maybe_build_fetched_image_messages(
    tool_results: list[dict],
    tool_calls_raw: list[dict],
    *,
    supports_vision: bool,
    read_image: Callable[[str], Awaitable[bytes | None]],
) -> list[dict]:
    """Build trailing vision-block user messages for fetched channel images.

    For each tool result whose tool name is ``fetch_channel_file``, whose
    parsed content has ``kind == "image"``, a raster ``mime_type``, and a
    ``path``: reads the bytes via ``read_image(path)`` and builds a user
    message with text + image_url blocks.

    Args:
        tool_results:  The list of tool-result dicts (role=tool) from execute_tool_calls.
        tool_calls_raw: The raw assistant tool_calls list (to map call_id -> name).
        supports_vision: Whether the current model supports native vision.
        read_image: Async callback ``path -> (bytes, mime) | None``.
                    Returns None or raises on failure -- both are handled gracefully.

    Returns:
        List of ``{role: user, content: [...]}`` messages to extend after tool_results.
        Empty list when not applicable or on any failure.
    """
    if not supports_vision:
        return []

    from surogates.harness.image_shrink import shrink_image_parts_in_messages

    extra: list[dict] = []

    for tr in tool_results:
        call_id = tr.get("tool_call_id") or ""
        tool_name = _tool_name_for_call_id(tool_calls_raw, call_id)
        if tool_name != "fetch_channel_file":
            continue

        # Parse content defensively.
        raw_content = tr.get("content") or ""
        try:
            if isinstance(raw_content, str):
                content_data = json.loads(raw_content)
            else:
                content_data = raw_content
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.debug("loop_vision_inject: could not parse tool result content for %s", call_id)
            continue

        if not isinstance(content_data, dict):
            continue
        if content_data.get("kind") != "image":
            continue
        path = content_data.get("path") or ""
        mime_type = (content_data.get("mime_type") or "").lower()
        filename = content_data.get("filename") or path.rsplit("/", 1)[-1] or "image"
        if not path or not _is_raster_mime(mime_type):
            continue

        # Read image bytes from storage (best-effort).
        try:
            img_bytes = await read_image(path)
        except Exception:
            logger.debug(
                "loop_vision_inject: storage read failed for path=%s; skipping injection",
                path, exc_info=True,
            )
            continue

        if not img_bytes:
            logger.debug("loop_vision_inject: no image bytes for path=%s; skipping", path)
            continue

        # Build the data URL with the fetch result's authoritative mime (already
        # raster-gated above); normalise the non-standard image/jpg alias.
        send_mime = "image/jpeg" if mime_type == "image/jpg" else mime_type
        b64 = base64.b64encode(img_bytes).decode("ascii")
        data_url = f"data:{send_mime};base64,{b64}"

        user_msg: dict[str, Any] = {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Fetched image: {filename}"},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "auto"},
                },
            ],
        }
        shrink_image_parts_in_messages([user_msg])
        extra.append(user_msg)

    return extra
