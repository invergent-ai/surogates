"""Harness-local vision analysis tool."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from surogates.harness.image_shrink import shrink_image_parts_in_messages
from surogates.harness.message_utils import message_to_dict
from surogates.storage.tenant import session_workspace_key
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.utils.url_safety import is_safe_url
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError, validate_path

logger = logging.getLogger(__name__)

_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_REDIRECTS = 5
_DEFAULT_PROMPT = "Describe the image. Include any visible text and important details."
# Long-edge caps before sending to the vision model. Anthropic resizes anything
# beyond ~1568 px server-side; OpenAI's ``low`` detail uses a single 512x512
# tile and ~85 tokens. Pre-resizing locally trims upload bytes and latency.
_VISION_MAX_DIMENSION_DEFAULT = 1568
_VISION_MAX_DIMENSION_LOW = 512
_VISION_MAX_BYTES = 1_000_000
_SUPPORTED_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})


VISION_ANALYZE_SCHEMA = ToolSchema(
    name="vision_analyze",
    description=(
        "Analyze an image from a workspace file path or HTTPS URL. "
        "Do NOT use this tool for images the user attached to their message — "
        "you can already see those directly. Only use this tool when you need "
        "to fetch and analyze an image from a URL or a file in the workspace."
    ),
    parameters={
        "type": "object",
        "properties": {
            "image": {
                "type": "string",
                "description": (
                    "Workspace-relative image path, HTTPS image URL, "
                    "or data:image base64 URL."
                ),
            },
            "question": {
                "type": "string",
                "description": "What to inspect or answer about the image.",
            },
            "detail": {
                "type": "string",
                "enum": ["auto", "low", "high"],
                "description": "Vision detail level for providers that support it.",
            },
        },
        "required": ["image"],
    },
)


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="vision_analyze",
        schema=VISION_ANALYZE_SCHEMA,
        handler=_vision_analyze_handler,
        toolset="vision",
        max_result_size=50_000,
    )


async def _vision_analyze_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    llm_client = kwargs.get("llm_client")
    if llm_client is None:
        return _json_error("vision_analyze requires a harness llm_client")

    image_ref = _get_image_ref(arguments)
    if not image_ref:
        return _json_error("Missing image reference")

    question = str(arguments.get("question") or _DEFAULT_PROMPT).strip() or _DEFAULT_PROMPT
    detail = str(arguments.get("detail") or "auto").strip().lower()
    if detail not in {"auto", "low", "high"}:
        detail = "auto"

    try:
        data_url, source_kind = await _image_ref_to_data_url(
            image_ref,
            workspace_path=kwargs.get("workspace_path"),
            storage=kwargs.get("storage"),
            session_id=kwargs.get("session_id"),
            session_config=kwargs.get("session_config"),
        )
    except ValueError as exc:
        return _json_error(str(exc))
    except WorkspaceSandboxError as exc:
        return _json_error(str(exc))
    except httpx.HTTPError as exc:
        logger.warning("vision_analyze image fetch failed: %s", exc)
        return _json_error(f"Failed to fetch image: {exc}")

    image_url: dict[str, Any] = {"url": data_url}
    if detail != "auto":
        image_url["detail"] = detail
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": image_url},
            ],
        }
    ]
    max_dimension = (
        _VISION_MAX_DIMENSION_LOW if detail == "low" else _VISION_MAX_DIMENSION_DEFAULT
    )
    shrink_image_parts_in_messages(
        messages,
        max_bytes=_VISION_MAX_BYTES,
        max_dimension=max_dimension,
    )

    model = (
        _configured_vision_model()
        or str(kwargs.get("model") or kwargs.get("session_model") or "surogate")
    )
    response = await llm_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
    )
    content = _extract_response_content(response)
    if not content:
        return _json_error("Vision model returned an empty response")

    return json.dumps(
        {
            "analysis": content,
            "model": getattr(response, "model", model),
            "source": source_kind,
            "usage": _extract_usage(response),
        },
        ensure_ascii=False,
    )


def _get_image_ref(arguments: dict[str, Any]) -> str:
    for key in ("image", "path", "image_path", "image_url", "url"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _configured_vision_model() -> str:
    from surogates.config import load_settings

    return str(getattr(load_settings().llm, "vision_model", "") or "").strip()


async def _image_ref_to_data_url(
    image_ref: str,
    *,
    workspace_path: str | None,
    storage: Any | None = None,
    session_id: Any | None = None,
    session_config: dict[str, Any] | None = None,
) -> tuple[str, str]:
    if image_ref.startswith("data:image/"):
        return _validate_data_url(image_ref), "data_url"

    parsed = urlparse(image_ref)
    if parsed.scheme in {"http", "https"}:
        data = await _download_image(image_ref)
        mime_type = _detect_mime_type(data, fallback="")
        if mime_type not in _SUPPORTED_MIME_TYPES:
            raise ValueError(f"Unsupported image MIME type: {mime_type or 'unknown'}")
        return _to_data_url(data, mime_type), "url"

    if storage is not None and session_id is not None:
        storage_bucket = (session_config or {}).get("storage_bucket")
        if storage_bucket:
            relative_path = _validate_workspace_storage_path(
                image_ref,
                workspace_path=workspace_path,
            )
            key = session_workspace_key(session_id, relative_path)
            try:
                data = await storage.read(storage_bucket, key)
            except KeyError:
                raise ValueError(f"Image file not found: {image_ref}") from None
            if len(data) > _MAX_IMAGE_BYTES:
                raise ValueError(
                    f"Image file is too large: {len(data)} bytes exceeds {_MAX_IMAGE_BYTES}"
                )
            mime_type = _detect_mime_type(
                data,
                fallback=mimetypes.guess_type(relative_path)[0] or "",
            )
            if mime_type not in _SUPPORTED_MIME_TYPES:
                raise ValueError(f"Unsupported image MIME type: {mime_type or 'unknown'}")
            return _to_data_url(data, mime_type), "workspace_file"

    path = Path(validate_path(workspace_path, image_ref))
    if not path.is_file():
        raise ValueError(f"Image file not found: {image_ref}")
    size = path.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image file is too large: {size} bytes exceeds {_MAX_IMAGE_BYTES}"
        )
    data = path.read_bytes()
    mime_type = _detect_mime_type(data, fallback=mimetypes.guess_type(path.name)[0] or "")
    if mime_type not in _SUPPORTED_MIME_TYPES:
        raise ValueError(f"Unsupported image MIME type: {mime_type or 'unknown'}")
    return _to_data_url(data, mime_type), "workspace_file"


def _validate_workspace_storage_path(
    image_ref: str,
    *,
    workspace_path: str | None,
) -> str:
    if workspace_path:
        workspace_root = PurePosixPath(workspace_path)
        ref_path = PurePosixPath(image_ref)
        try:
            image_ref = ref_path.relative_to(workspace_root).as_posix()
        except ValueError:
            pass

    path = PurePosixPath(image_ref)
    if path.is_absolute() or not path.parts or any(part == ".." for part in path.parts):
        raise WorkspaceSandboxError(f"Path traversal blocked: {image_ref}")
    return path.as_posix()


def _validate_data_url(data_url: str) -> str:
    header, sep, encoded = data_url.partition(",")
    if not sep or ";base64" not in header:
        raise ValueError("Only base64 data:image URLs are supported")
    mime_type = header.removeprefix("data:").split(";", 1)[0].lower()
    if mime_type not in _SUPPORTED_MIME_TYPES:
        raise ValueError(f"Unsupported image MIME type: {mime_type or 'unknown'}")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 image data") from exc
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image data URL is too large: {len(raw)} bytes exceeds {_MAX_IMAGE_BYTES}"
        )
    return data_url


async def _download_image(url: str) -> bytes:
    current = url
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            if not is_safe_url(current):
                raise ValueError("Blocked unsafe image URL")
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Image URL redirected without a Location header")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > _MAX_IMAGE_BYTES:
                    raise ValueError(
                        f"Image download is too large: {length} bytes exceeds {_MAX_IMAGE_BYTES}"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_IMAGE_BYTES:
                        raise ValueError(
                            f"Image download is too large: exceeds {_MAX_IMAGE_BYTES} bytes"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ValueError("Image URL redirected too many times")


def _detect_mime_type(data: bytes, *, fallback: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return fallback.lower()


def _to_data_url(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_response_content(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    message_dict = message_to_dict(message)
    content = message_dict.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part.strip() for part in parts if part and part.strip())
    return str(content).strip() if content is not None else ""


def _extract_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


def _json_error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)
