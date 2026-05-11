"""Image payload shrinking for multimodal LLM requests."""

from __future__ import annotations

import base64
import binascii
import io
import logging
from typing import Any

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

_DATA_IMAGE_PREFIX = "data:image/"
_DEFAULT_MAX_IMAGE_BYTES = 4_500_000
_MIN_DIMENSION = 256


def shrink_image_parts_in_messages(
    messages: list[dict[str, Any]],
    *,
    max_bytes: int = _DEFAULT_MAX_IMAGE_BYTES,
    max_dimension: int | None = None,
) -> int:
    """Normalize oversized data-URL image parts in-place.

    Two modes:

    * ``max_dimension is None`` (retry path) -- byte-only shrink. Images
      already under ``max_bytes`` are left untouched. This preserves
      retry-on-image-too-large semantics for ``llm_call``.
    * ``max_dimension`` set (pre-flight path) -- always normalize: cap the
      long edge to ``max_dimension`` and re-encode to JPEG. Used by the
      vision tool so requests do not ship oversized pixels or PNG bulk.

    Returns the number of image parts changed. URL-based images are left
    untouched because the provider, not Surogates, fetches those bytes.
    """
    changed = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                url = image_url.get("url")
                shrunk = _shrink_data_url(
                    url,
                    max_bytes=max_bytes,
                    max_dimension=max_dimension,
                )
                if shrunk is not None and shrunk != url:
                    image_url["url"] = shrunk
                    changed += 1
    return changed


def _shrink_data_url(
    url: Any,
    *,
    max_bytes: int,
    max_dimension: int | None = None,
) -> str | None:
    if not isinstance(url, str) or not url.startswith(_DATA_IMAGE_PREFIX):
        return None
    header, sep, encoded = url.partition(",")
    if not sep or ";base64" not in header:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None

    if max_dimension is None:
        # Retry path: only act when the image exceeds the byte budget.
        if len(raw) <= max_bytes:
            return url
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if max_dimension is not None:
                width, height = image.size
                is_jpeg = header.startswith("data:image/jpeg")
                if (
                    max(width, height) <= max_dimension
                    and len(raw) <= max_bytes
                    and is_jpeg
                ):
                    return url
            shrunk = _recompress_image(
                image,
                max_bytes=max_bytes,
                max_dimension=max_dimension,
            )
    except (OSError, UnidentifiedImageError) as exc:
        logger.debug("Failed to shrink image payload: %s", exc)
        return None

    if shrunk is None:
        return None
    if max_dimension is None and len(shrunk) >= len(raw):
        return None
    return "data:image/jpeg;base64," + base64.b64encode(shrunk).decode("ascii")


def _recompress_image(
    image: Image.Image,
    *,
    max_bytes: int,
    max_dimension: int | None = None,
) -> bytes | None:
    rgb = _to_rgb(image)

    if max_dimension is not None and max(rgb.size) > max_dimension:
        rgb.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

    width, height = rgb.size
    scale = 1.0

    while min(width, height) >= _MIN_DIMENSION:
        candidate = rgb.copy()
        if scale < 1.0:
            candidate.thumbnail(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.Resampling.LANCZOS,
            )
        for quality in (85, 75, 65, 55, 45):
            output = io.BytesIO()
            candidate.save(output, format="JPEG", quality=quality, optimize=True)
            data = output.getvalue()
            if len(data) <= max_bytes:
                return data
        scale *= 0.75

    return None


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    if image.mode != "RGB":
        return image.convert("RGB")
    return image.copy()
