"""How to ask an image provider for an image.

Providers disagree on the request.  OpenRouter wants chat completions
with ``modalities`` and an ``image_config``; gateways that add the
modalities themselves (llm.buzz) refuse ``modalities`` with a 400; image
only models are also served by an OpenAI-style ``/images/generations``,
the only surface with a transparent background.  The model's own entry
on the provider's ``/models`` (input and output modalities) picks the
first shape to try; a rejected shape costs nothing, so the next one is
tried, and the shape that worked is remembered for the next call.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from surogates.harness.message_utils import message_to_dict
from surogates.tools.builtin.vision import _download_image, _extract_response_content

logger = logging.getLogger(__name__)

CHAT = "chat"
CHAT_PLAIN = "chat_plain"
IMAGES = "images"

_INFO_TTL = 3600.0
_UNKNOWN_TTL = 300.0
_MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
# Statuses a provider answers when it does not take this request shape
# (an unknown parameter, a modality it does not emit, a route it lacks).
# Rejections are not billed, so trying the next shape is free.
_SHAPE_REJECTED = frozenset({400, 404, 405, 415, 422})
_SIZE_TIERS = {"0.5K": 512, "1K": 1024, "2K": 2048, "4K": 4096}
_FORMAT_MIME = {
    "PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif",
}


@dataclass(frozen=True)
class ImageModelInfo:
    """What the provider's ``/models`` says about the model; unlisted = unknown."""

    listed: bool = False
    makes_images: bool = True
    makes_text: bool = True
    takes_images: bool = True


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    has_alpha: bool
    text: str = ""
    model: str = ""
    cost_cents: int | None = None


class ImageRequestError(Exception):
    """The provider refused or failed; ``message`` is safe to show the model."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


_listing_cache: dict[str, tuple[float, dict[str, ImageModelInfo]]] = {}
_shape_cache: dict[tuple[str, str], str] = {}


def _key(client: Any, model: str) -> tuple[str, str]:
    return str(getattr(client, "base_url", "")), model


async def model_info(client: Any, model: str) -> ImageModelInfo:
    """One ``/models`` listing per provider serves all its models; a model it
    does not list is asked again sooner, in case it was added since."""
    base_url = str(getattr(client, "base_url", ""))
    now = time.monotonic()
    cached = _listing_cache.get(base_url)
    if cached is not None:
        info = cached[1].get(model)
        if now - cached[0] < (_INFO_TTL if info is not None else _UNKNOWN_TTL):
            return info or ImageModelInfo()
    listing = await _fetch_listing(client)
    _listing_cache[base_url] = (now, listing)
    return listing.get(model) or ImageModelInfo()


async def _fetch_listing(client: Any) -> dict[str, ImageModelInfo]:
    try:
        raw = await client.get(
            "models", cast_to=object,
            options={"params": {"output_modalities": "image"}, "timeout": 10},
        )
    except Exception as exc:  # noqa: BLE001 — unknown capabilities fall back to the defaults
        logger.info("image model list unavailable at %s: %s", getattr(client, "base_url", ""), exc)
        return {}
    rows = raw.get("data") if isinstance(raw, dict) else raw
    listing: dict[str, ImageModelInfo] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        arch = row.get("architecture") or {}
        outputs = arch.get("output_modalities") or row.get("output_modalities") or []
        inputs = arch.get("input_modalities") or row.get("input_modalities") or []
        listing.setdefault(row["id"], ImageModelInfo(
            listed=True,
            makes_images="image" in outputs,
            makes_text="text" in outputs,
            takes_images="image" in inputs,
        ))
    return listing


def shape_order(
    client: Any, model: str, info: ImageModelInfo, *, with_inputs: bool, transparent: bool,
) -> list[str]:
    if with_inputs:
        order = [CHAT, CHAT_PLAIN]
    elif transparent:
        order = [IMAGES, CHAT, CHAT_PLAIN]
    elif info.listed and not info.makes_text:
        order = [CHAT, IMAGES, CHAT_PLAIN]
    else:
        order = [CHAT, CHAT_PLAIN, IMAGES]
    remembered = _shape_cache.get(_key(client, model))
    if remembered in order and not (transparent and remembered != IMAGES):
        order.remove(remembered)
        order.insert(0, remembered)
    return order


async def generate(
    client: Any,
    model: str,
    *,
    prompt: str,
    input_images: list[str],
    aspect_ratio: str,
    image_size: str,
    transparent: bool,
    info: ImageModelInfo,
) -> GeneratedImage:
    """Try each shape in turn; raise the last refusal when none takes it."""
    last: ImageRequestError | None = None
    for shape in shape_order(
        client, model, info, with_inputs=bool(input_images), transparent=transparent,
    ):
        try:
            if shape == IMAGES:
                image = await _via_images(
                    client, model, prompt=prompt, aspect_ratio=aspect_ratio,
                    transparent=transparent,
                )
            else:
                image = await _via_chat(
                    client, model, prompt=prompt, input_images=input_images,
                    extra_body=_chat_body(shape, info, aspect_ratio, image_size),
                )
        except ImageRequestError as exc:
            last = exc
            if exc.status in _SHAPE_REJECTED:
                continue
            raise
        _shape_cache[_key(client, model)] = shape
        return image
    assert last is not None
    raise last


def _chat_body(shape: str, info: ImageModelInfo, aspect_ratio: str, image_size: str) -> dict[str, Any]:
    if shape == CHAT_PLAIN:
        return {}
    body: dict[str, Any] = {
        "modalities": ["image", "text"] if info.makes_text else ["image"],
    }
    image_config = {
        name: value
        for name, value in (("aspect_ratio", aspect_ratio), ("image_size", image_size))
        if value
    }
    if image_config:
        body["image_config"] = image_config
    return body


async def _via_chat(
    client: Any, model: str, *, prompt: str, input_images: list[str], extra_body: dict[str, Any],
) -> GeneratedImage:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": url}} for url in input_images]
    try:
        response = await client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": content}], extra_body=extra_body,
        )
    except Exception as exc:  # noqa: BLE001 — every provider failure becomes one short reason
        raise _refusal(exc) from exc
    url = _first_image_url(response)
    if not url:
        raise ImageRequestError("The image model returned no image")
    data = await _image_bytes(url)
    # Decoding a multi-megapixel image is CPU work: off the event loop.
    return await asyncio.to_thread(
        inspect, data, text=_extract_response_content(response),
        model=str(getattr(response, "model", "") or model), cost_cents=_reported_cents(response),
    )


async def _via_images(
    client: Any, model: str, *, prompt: str, aspect_ratio: str, transparent: bool,
) -> GeneratedImage:
    extra: dict[str, Any] = {"background": "transparent", "output_format": "png"} if transparent else {}
    # A ratio goes as itself, so the gateway picks the model's own size for it
    # (Gemini serves 1344x768 for 16:9, not 1536x1024). A provider that does not
    # take the field refuses it unbilled, and gets the nearest OpenAI size.
    asks: list[dict[str, Any]] = []
    if parse_ratio(aspect_ratio) is not None:
        asks.append({"extra_body": {**extra, "aspect_ratio": aspect_ratio}})
    asks.append({"size": _images_size(aspect_ratio), "extra_body": extra})
    for i, ask in enumerate(asks):
        try:
            response = await client.images.generate(model=model, prompt=prompt, n=1, **ask)
            break
        except Exception as exc:  # noqa: BLE001
            refusal = _refusal(exc)
            if i + 1 < len(asks) and refusal.status in _SHAPE_REJECTED:
                continue
            raise refusal from exc
    entries = getattr(response, "data", None) or []
    first = entries[0] if entries else None
    b64 = getattr(first, "b64_json", None) if first is not None else None
    url = getattr(first, "url", None) if first is not None else None
    if b64:
        data = _decode_b64(b64)
    elif url:
        data = await _image_bytes(url)
    else:
        raise ImageRequestError("The image model returned no image")
    return await asyncio.to_thread(inspect, data, model=model, cost_cents=_reported_cents(response))


def _images_size(aspect_ratio: str) -> str:
    ratio = parse_ratio(aspect_ratio)
    if ratio is None or 0.83 <= ratio <= 1.2:
        return "1024x1024"
    return "1536x1024" if ratio > 1 else "1024x1536"


def parse_ratio(aspect_ratio: str) -> float | None:
    width, _, height = (aspect_ratio or "").partition(":")
    try:
        ratio = float(width) / float(height)
    except (ValueError, ZeroDivisionError):
        return None
    return ratio if ratio > 0 else None


def inspect(
    data: bytes, *, text: str = "", model: str = "", cost_cents: int | None = None,
) -> GeneratedImage:
    """Decode the bytes; the format, size and transparency are read, never assumed."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            mime_type = _FORMAT_MIME.get(image.format or "")
            if mime_type is None:
                raise ImageRequestError(
                    f"The image model returned a {image.format or 'unknown'} file, which cannot be saved as an image"
                )
            width, height = image.size
            has_alpha = _has_transparency(image)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageRequestError("The image model returned data that is not an image") from exc
    return GeneratedImage(
        data=data, mime_type=mime_type, width=width, height=height, has_alpha=has_alpha,
        text=text, model=model, cost_cents=cost_cents,
    )


def _has_transparency(image: Any) -> bool:
    if image.mode in ("RGBA", "LA", "PA"):
        return image.getchannel("A").getextrema()[0] < 255
    return "transparency" in image.info


def shortfalls(
    image: GeneratedImage, *, aspect_ratio: str, image_size: str, transparent: bool,
) -> list[str]:
    """Where the image differs from what was asked, in words the model can relay."""
    found = []
    wanted = parse_ratio(aspect_ratio)
    if wanted is not None and abs(math.log((image.width / image.height) / wanted)) > 0.05:
        found.append(
            f"Asked for {aspect_ratio}, got {image.width}x{image.height}: "
            "this image model did not apply the aspect ratio."
        )
    target = _SIZE_TIERS.get(image_size)
    if target is not None and max(image.width, image.height) < 0.75 * target:
        found.append(
            f"Asked for {image_size}, got {image.width}x{image.height}: "
            "this image model returned a smaller image."
        )
    if transparent and not image.has_alpha:
        found.append(
            "Asked for a transparent background, but the image has none: "
            "this image model cannot make transparent images."
        )
    return found


def _first_message(response: Any) -> Any:
    choices = getattr(response, "choices", None) or []
    return getattr(choices[0], "message", None) if choices else None


def _first_image_url(response: Any) -> str:
    """OpenRouter's ``message.images[]``, else an image part of the content."""
    message = _first_message(response)
    if message is None:
        return ""
    images = getattr(message, "images", None) or message_to_dict(message).get("images")
    candidates = list(images or [])
    content = getattr(message, "content", None)
    if isinstance(content, list):
        candidates += [part for part in content if _part_type(part) == "image_url"]
    for item in candidates:
        if not isinstance(item, dict):
            item = message_to_dict(item) if hasattr(item, "model_dump") else {}
        url = str(((item.get("image_url") or {}).get("url")) or "")
        if url:
            return url
    return ""


def _part_type(part: Any) -> str:
    return str(part.get("type") if isinstance(part, dict) else getattr(part, "type", ""))


async def _image_bytes(url: str) -> bytes:
    if url.startswith("data:"):
        return _decode_b64(url.partition(",")[2])
    if not url.startswith("https://"):
        raise ImageRequestError("The image model returned an image link that cannot be fetched")
    try:
        return await _download_image(url, max_bytes=_MAX_DOWNLOAD_BYTES)
    except ValueError as exc:
        raise ImageRequestError(f"The generated image could not be downloaded: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ImageRequestError("The generated image could not be downloaded; try again") from exc


def _decode_b64(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageRequestError("The image model returned invalid base64 image data") from exc


def _reported_cents(response: Any) -> int | None:
    """The provider's own cost for this call (OpenRouter reports ``usage.cost``)."""
    usage = getattr(response, "usage", None)
    cost = getattr(usage, "cost", None)
    if cost is None and hasattr(response, "model_dump"):
        cost = ((response.model_dump().get("usage") or {}) or {}).get("cost")
    try:
        return max(0, math.ceil(float(cost) * 100)) if cost is not None else None
    except (TypeError, ValueError):
        return None


def _refusal(exc: Exception) -> ImageRequestError:
    from surogates.harness.error_classifier import extract_status_code

    status = extract_status_code(exc)
    said = _upstream_message(exc)
    if status in (401, 403):
        reason = "the image provider refused this agent's credentials"
    elif status == 429:
        reason = "the image provider is rate limiting; try again in a minute"
    elif status is None or status >= 500:
        reason = "the image provider is unavailable right now; try again shortly"
    elif status == 404:
        reason = "the image model is not available at this provider for this request"
    else:
        reason = "the image provider rejected the request"
    return ImageRequestError(
        f"Image generation failed: {reason}" + (f" ({said})" if said else "") + ".",
        status=status,
    )


def _upstream_message(exc: Exception) -> str:
    """The provider's own one-line reason, never its raw JSON."""
    from surogates.harness.error_classifier import extract_error_body

    body = extract_error_body(exc)
    error = body.get("error", body)
    message = error.get("message") if isinstance(error, dict) else error
    if message is None and isinstance(body.get("detail"), str):
        message = body["detail"]
    text = " ".join(str(message or "").split())
    return text[:200].rstrip(".")
