"""Image and video generation tools (OpenRouter-shaped APIs).

``generate_image`` calls a chat-completions endpoint with
``modalities: ["image", "text"]`` (how OpenRouter exposes image models)
through the per-session image client.  ``generate_video`` drives the
async ``POST /videos`` job API over raw httpx: submit, poll, download.
Both write the result into the session workspace (local path and/or
object storage, mirroring browser screenshots) and return the
workspace-relative path — directly usable as ``media_path`` in a
delivery-outbox payload.

Endpoint resolution happens harness-side: the worker composes a
:class:`MediaGenConfig` per session and threads it through the tool
executor kwargs (the same chain ``vision_llm_client`` rides).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import httpx

from surogates.harness.message_utils import message_to_dict
from surogates.storage.tenant import prefixed_session_workspace_key
from surogates.tools.builtin.vision import (
    _extract_response_content,
    _image_ref_to_data_url,
)
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError, validate_path

logger = logging.getLogger(__name__)

_MAX_VIDEO_BYTES = 512 * 1024 * 1024

_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass(frozen=True)
class MediaGenConfig:
    """Per-session media-generation wiring threaded into tool kwargs.

    ``image_client`` is an ``AsyncOpenAI`` (or duck-typed equivalent)
    already pointed at the image endpoint; video carries raw endpoint
    fields because the ``/videos`` job API is called over httpx.
    """

    image_client: Any | None = None
    image_model: str = ""
    video_model: str = ""
    video_base_url: str = ""
    video_api_key: str = ""
    video_timeout: int = 600
    video_poll_interval: int = 10


GENERATE_IMAGE_SCHEMA = ToolSchema(
    name="generate_image",
    description=(
        "Generate an image from a text prompt and save it into the session "
        "workspace. Optionally guide the generation with input images "
        "(image-to-image). Returns the workspace-relative file path of the "
        "generated image."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text description of the image to generate.",
            },
            "aspect_ratio": {
                "type": "string",
                "description": "Aspect ratio, e.g. '1:1', '16:9', '9:16', '21:9'.",
            },
            "image_size": {
                "type": "string",
                "enum": ["0.5K", "1K", "2K", "4K"],
                "description": "Output resolution tier (provider default: 1K).",
            },
            "input_images": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional reference images for image-to-image: "
                    "workspace-relative paths, HTTPS URLs, or data URLs."
                ),
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Optional workspace-relative output path. Defaults to "
                    "media/images/image-<timestamp>-<id>.<ext>."
                ),
            },
        },
        "required": ["prompt"],
    },
)


GENERATE_VIDEO_SCHEMA = ToolSchema(
    name="generate_video",
    description=(
        "Generate a video from a text prompt and save it into the session "
        "workspace. Rendering takes minutes; the call blocks until the video "
        "is ready. Optionally animate from a first-frame image "
        "(image-to-video). Returns the workspace-relative file path."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text description of the video to generate.",
            },
            "duration": {
                "type": "integer",
                "description": "Video length in seconds (model-dependent).",
            },
            "resolution": {
                "type": "string",
                "enum": ["480p", "720p", "1080p", "1K", "2K", "4K"],
                "description": "Output resolution.",
            },
            "aspect_ratio": {
                "type": "string",
                "description": "Aspect ratio, e.g. '16:9', '9:16', '1:1'.",
            },
            "first_frame_image": {
                "type": "string",
                "description": (
                    "Optional first-frame image for image-to-video: "
                    "workspace-relative path, HTTPS URL, or data URL."
                ),
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Optional workspace-relative output path. Defaults to "
                    "media/videos/video-<timestamp>-<id>.mp4."
                ),
            },
        },
        "required": ["prompt"],
    },
)


def register(registry: ToolRegistry) -> None:
    """Register both tools unconditionally.

    Convention (vision, kb_tools): always register, gate at call time —
    an unconfigured tool returns an explanatory error and the
    per-session ``available_tools`` filter controls model visibility.
    """
    registry.register(
        name="generate_image",
        schema=GENERATE_IMAGE_SCHEMA,
        handler=_generate_image_handler,
        toolset="media_gen",
        max_result_size=10_000,
    )
    registry.register(
        name="generate_video",
        schema=GENERATE_VIDEO_SCHEMA,
        handler=_generate_video_handler,
        toolset="media_gen",
        max_result_size=10_000,
    )


async def _generate_image_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    cfg = kwargs.get("media_gen")
    client = getattr(cfg, "image_client", None)
    model = str(getattr(cfg, "image_model", "") or "")
    if client is None or not model:
        return _json_error(
            "generate_image is not available: no image model is configured"
        )

    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        return _json_error("Missing prompt")

    # Validate a user-supplied output path BEFORE spending provider quota.
    try:
        output_path = _normalize_output_path(arguments.get("output_path"), default="")
    except WorkspaceSandboxError as exc:
        return _json_error(str(exc))

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    input_images = arguments.get("input_images") or []
    if isinstance(input_images, str):
        input_images = [input_images]
    for ref in input_images:
        try:
            data_url, _ = await _image_ref_to_data_url(
                str(ref),
                workspace_path=kwargs.get("workspace_path"),
                storage=kwargs.get("storage"),
                session_id=kwargs.get("session_id"),
                session_config=kwargs.get("session_config"),
            )
        except (ValueError, WorkspaceSandboxError) as exc:
            return _json_error(str(exc))
        except httpx.HTTPError as exc:
            return _json_error(f"Failed to fetch input image: {exc}")
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    extra_body: dict[str, Any] = {"modalities": ["image", "text"]}
    for field_name in ("aspect_ratio", "image_size"):
        value = str(arguments.get(field_name) or "").strip()
        if value:
            extra_body[field_name] = value

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            extra_body=extra_body,
        )
    except Exception as exc:  # noqa: BLE001 — provider errors become tool errors
        return _json_error(f"Image generation failed: {exc}")

    image_url = _first_generated_image_url(response)
    if not image_url.startswith("data:image/"):
        return _json_error("The image model returned no image")

    header, _, encoded = image_url.partition(",")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return _json_error("The image model returned invalid base64 image data")

    mime_type = header.removeprefix("data:").split(";", 1)[0].lower()
    extension = _MIME_EXTENSIONS.get(mime_type, "png")
    relative_path = output_path or _default_media_path("image", extension)

    saved = await _save_media_bytes(
        data,
        relative_path=relative_path,
        workspace_path=kwargs.get("workspace_path"),
        storage=kwargs.get("storage"),
        session_id=kwargs.get("session_id"),
        session_config=kwargs.get("session_config"),
    )
    if not saved:
        return _json_error(
            "workspace_unavailable: generate_image requires a session "
            "workspace destination"
        )

    result: dict[str, Any] = {
        "path": relative_path,
        "model": getattr(response, "model", model),
    }
    text = _extract_response_content(response)
    if text:
        result["text"] = text
    return json.dumps(result, ensure_ascii=False)


async def _generate_video_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    cfg = kwargs.get("media_gen")
    model = str(getattr(cfg, "video_model", "") or "")
    base_url = str(getattr(cfg, "video_base_url", "") or "")
    api_key = str(getattr(cfg, "video_api_key", "") or "")
    timeout = int(getattr(cfg, "video_timeout", 600))
    poll_interval = max(1, int(getattr(cfg, "video_poll_interval", 10)))
    if not model or not base_url:
        return _json_error(
            "generate_video is not available: no video model is configured"
        )

    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        return _json_error("Missing prompt")

    try:
        relative_path = _normalize_output_path(
            arguments.get("output_path"),
            default=_default_media_path("video", "mp4"),
        )
    except WorkspaceSandboxError as exc:
        return _json_error(str(exc))

    body: dict[str, Any] = {"model": model, "prompt": prompt}
    duration = arguments.get("duration")
    if duration:
        body["duration"] = int(duration)
    for field_name in ("resolution", "aspect_ratio"):
        value = str(arguments.get(field_name) or "").strip()
        if value:
            body[field_name] = value

    first_frame = str(arguments.get("first_frame_image") or "").strip()
    if first_frame:
        try:
            data_url, _ = await _image_ref_to_data_url(
                first_frame,
                workspace_path=kwargs.get("workspace_path"),
                storage=kwargs.get("storage"),
                session_id=kwargs.get("session_id"),
                session_config=kwargs.get("session_config"),
            )
        except (ValueError, WorkspaceSandboxError) as exc:
            return _json_error(str(exc))
        except httpx.HTTPError as exc:
            return _json_error(f"Failed to fetch first frame image: {exc}")
        body["frame_images"] = [
            {
                "type": "image_url",
                "image_url": {"url": data_url},
                "frame_type": "first_frame",
            }
        ]

    videos_url = f"{base_url.rstrip('/')}/videos"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0), headers=headers,
        ) as client:
            response = await client.post(videos_url, json=body)
            response.raise_for_status()
            status_data: dict[str, Any] = response.json()
            job_id = str(status_data.get("id") or "")
            polling_url = (
                str(status_data.get("polling_url") or "")
                or f"{videos_url}/{job_id}"
            )

            deadline = asyncio.get_running_loop().time() + timeout
            while str(status_data.get("status") or "") not in {"completed", "failed"}:
                if asyncio.get_running_loop().time() >= deadline:
                    return _json_error(
                        f"Video generation timed out after {timeout}s; job "
                        f"{job_id} may still complete upstream ({polling_url})"
                    )
                await asyncio.sleep(poll_interval)
                poll_response = await client.get(polling_url)
                poll_response.raise_for_status()
                status_data = poll_response.json()

            if status_data.get("status") == "failed":
                detail = (
                    status_data.get("error")
                    or status_data.get("failure_reason")
                    or "unknown error"
                )
                return _json_error(f"Video generation failed: {detail}")

            urls = status_data.get("unsigned_urls") or []
            if not urls:
                return _json_error(
                    "Video job completed but returned no download URL"
                )
            data = await _download_video(client, str(urls[0]))
    except httpx.HTTPError as exc:
        return _json_error(f"Video generation request failed: {exc}")
    except ValueError as exc:
        return _json_error(str(exc))

    saved = await _save_media_bytes(
        data,
        relative_path=relative_path,
        workspace_path=kwargs.get("workspace_path"),
        storage=kwargs.get("storage"),
        session_id=kwargs.get("session_id"),
        session_config=kwargs.get("session_config"),
    )
    if not saved:
        return _json_error(
            "workspace_unavailable: generate_video requires a session "
            "workspace destination"
        )

    result: dict[str, Any] = {"path": relative_path, "model": model, "job_id": job_id}
    usage = status_data.get("usage") or {}
    if usage.get("cost") is not None:
        result["cost"] = usage["cost"]
    return json.dumps(result, ensure_ascii=False)


def _first_generated_image_url(response: Any) -> str:
    """Extract the first generated image data URL from a completion.

    OpenRouter returns generated images as
    ``message.images[].image_url.url``.  ``images`` is a provider
    extension, so read it from the duck-typed attribute first and the
    serialized dict second (``model_dump`` keeps pydantic extras).
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    images = getattr(message, "images", None)
    if not images:
        images = message_to_dict(message).get("images")
    if not images:
        return ""
    first = images[0]
    if not isinstance(first, dict):
        first = message_to_dict(first) if hasattr(first, "model_dump") else {}
    return str(((first.get("image_url") or {}).get("url")) or "")


async def _download_video(client: httpx.AsyncClient, url: str) -> bytes:
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > _MAX_VIDEO_BYTES:
                raise ValueError(
                    f"Video download exceeds {_MAX_VIDEO_BYTES} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)


def _default_media_path(kind: str, extension: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"media/{kind}s/{kind}-{timestamp}-{uuid4().hex[:8]}.{extension}"


def _normalize_output_path(raw: Any, *, default: str) -> str:
    """Clean a user-supplied workspace-relative output path.

    Empty input yields *default*.  Absolute paths are re-rooted into the
    workspace by stripping the leading slash; ``..`` traversal is
    rejected outright.
    """
    path = str(raw or "").strip().lstrip("/")
    if not path:
        return default
    parts = PurePosixPath(path)
    if any(part == ".." for part in parts.parts):
        raise WorkspaceSandboxError(f"Path traversal blocked: {path}")
    return parts.as_posix()


async def _save_media_bytes(
    data: bytes,
    *,
    relative_path: str,
    workspace_path: str | None,
    storage: Any | None,
    session_id: Any | None,
    session_config: dict[str, Any] | None,
) -> bool:
    """Dual-write generated bytes: local workspace and/or object storage.

    Mirrors the browser-screenshot pattern — PROD workspaces are
    storage-backed and the worker may have no local directory, local dev
    has no storage backend.  Returns ``True`` if at least one
    destination accepted the bytes.
    """
    saved = False
    if workspace_path:
        try:
            target = Path(validate_path(workspace_path, relative_path))
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = Path(f"{target}.tmp")
            tmp_path.write_bytes(data)
            os.replace(tmp_path, target)
            saved = True
        except (OSError, WorkspaceSandboxError) as exc:
            logger.warning(
                "Could not save generated media to workspace path %s: %s",
                relative_path, exc,
            )
    bucket = (session_config or {}).get("storage_bucket")
    if storage is not None and session_id is not None and bucket:
        key = prefixed_session_workspace_key(session_config, session_id, relative_path)
        try:
            await storage.write(bucket, key, data)
            saved = True
        except Exception as exc:  # noqa: BLE001 — storage failure must not crash the tool
            logger.warning(
                "Could not save generated media to storage %s/%s: %s",
                bucket, key, exc,
            )
    return saved


def _json_error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)
