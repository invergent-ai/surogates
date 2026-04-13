"""Media cache utilities for channel adapters.

Downloads and caches images, audio, and documents from messaging platforms
so tools can reference them by local file path.  Supports retry with
exponential backoff and SSRF protection.

Ported from Hermes ``gateway/platforms/base.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache base directory
# ---------------------------------------------------------------------------

MEDIA_CACHE_BASE = Path(os.getenv(
    "SUROGATES_MEDIA_CACHE", "/tmp/surogates/cache",
))

# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------


def safe_url_for_log(url: str, max_len: int = 80) -> str:
    """Truncate a URL for safe logging (no credentials)."""
    if len(url) <= max_len:
        return url
    return url[:max_len] + "..."


def _is_safe_url(url: str) -> bool:
    """Return True if *url* does not target a private/internal network."""
    from surogates.tools.utils.url_safety import is_safe_url
    return is_safe_url(url)


async def _ssrf_redirect_guard(response: httpx.Response) -> None:
    """Event hook that blocks redirects to private/internal addresses."""
    if response.is_redirect:
        location = response.headers.get("location", "")
        if location and not _is_safe_url(location):
            raise ValueError(
                f"Blocked redirect to internal address: {safe_url_for_log(location)}"
            )


# ---------------------------------------------------------------------------
# Image cache
# ---------------------------------------------------------------------------

IMAGE_CACHE_DIR = MEDIA_CACHE_BASE / "images"


def get_image_cache_dir() -> Path:
    """Return the image cache directory, creating it if it doesn't exist."""
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGE_CACHE_DIR


def _looks_like_image(data: bytes) -> bool:
    """Return True if *data* starts with a known image magic-byte sequence."""
    if len(data) < 4:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:2] == b"BM":
        return True
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    return False


def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    """Save raw image bytes to the cache and return the absolute file path.

    Raises ``ValueError`` if *data* does not look like a valid image.
    """
    if not _looks_like_image(data):
        snippet = data[:80].decode("utf-8", errors="replace")
        raise ValueError(
            f"Refusing to cache non-image data as {ext} "
            f"(starts with: {snippet!r})"
        )
    cache_dir = get_image_cache_dir()
    filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


async def _download_and_cache(
    url: str,
    ext: str,
    cache_fn: Any,
    accept: str,
    label: str,
    retries: int = 2,
) -> str:
    """Download from URL with retry and cache via *cache_fn*."""
    if not _is_safe_url(url):
        raise ValueError(f"Blocked unsafe URL (SSRF protection): {safe_url_for_log(url)}")

    last_exc: Exception | None = None
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        event_hooks={"response": [_ssrf_redirect_guard]},
    ) as client:
        for attempt in range(retries + 1):
            try:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; SurogatesAgent/1.0)",
                        "Accept": accept,
                    },
                )
                response.raise_for_status()
                return cache_fn(response.content, ext)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                    raise
                if attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    logger.debug(
                        "%s cache retry %d/%d for %s (%.1fs): %s",
                        label, attempt + 1, retries, safe_url_for_log(url), wait, exc,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise
    raise last_exc  # type: ignore[misc]


async def cache_image_from_url(url: str, ext: str = ".jpg", retries: int = 2) -> str:
    """Download an image from a URL and save it to the local cache."""
    return await _download_and_cache(url, ext, cache_image_from_bytes, "image/*,*/*;q=0.8", "Image", retries)


def _cleanup_cache_dir(cache_dir: Path, max_age_hours: int) -> int:
    """Delete files older than *max_age_hours* from *cache_dir*."""
    if not cache_dir.is_dir():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def cleanup_image_cache(max_age_hours: int = 24) -> int:
    """Delete cached images older than *max_age_hours*.  Returns count removed."""
    return _cleanup_cache_dir(get_image_cache_dir(), max_age_hours)


# ---------------------------------------------------------------------------
# Audio cache
# ---------------------------------------------------------------------------

AUDIO_CACHE_DIR = MEDIA_CACHE_BASE / "audio"


def get_audio_cache_dir() -> Path:
    """Return the audio cache directory, creating it if it doesn't exist."""
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return AUDIO_CACHE_DIR


def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
    """Save raw audio bytes to the cache and return the absolute file path."""
    cache_dir = get_audio_cache_dir()
    filename = f"audio_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


async def cache_audio_from_url(url: str, ext: str = ".ogg", retries: int = 2) -> str:
    """Download audio from a URL and save it to the local cache."""
    return await _download_and_cache(url, ext, cache_audio_from_bytes, "audio/*,*/*;q=0.8", "Audio", retries)


# ---------------------------------------------------------------------------
# Document cache
# ---------------------------------------------------------------------------

DOCUMENT_CACHE_DIR = MEDIA_CACHE_BASE / "documents"

SUPPORTED_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def get_document_cache_dir() -> Path:
    """Return the document cache directory, creating it if it doesn't exist."""
    DOCUMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return DOCUMENT_CACHE_DIR


def cache_document_from_bytes(data: bytes, filename: str) -> str:
    """Save raw document bytes to the cache and return the absolute file path.

    The cached filename preserves the original human-readable name with a
    unique prefix: ``doc_{uuid12}_{original_filename}``.

    Raises ``ValueError`` if the sanitized path escapes the cache directory.
    """
    cache_dir = get_document_cache_dir()
    # Sanitize: strip directory components, null bytes, and control characters.
    safe_name = Path(filename).name if filename else "document"
    safe_name = safe_name.replace("\x00", "").strip()
    if not safe_name or safe_name in (".", ".."):
        safe_name = "document"
    cached_name = f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
    filepath = cache_dir / cached_name
    # Final safety check: ensure path stays inside cache dir.
    if not filepath.resolve().is_relative_to(cache_dir.resolve()):
        raise ValueError(f"Path traversal denied: {filename}")
    filepath.write_bytes(data)
    return str(filepath)


def cleanup_document_cache(max_age_hours: int = 24) -> int:
    """Delete cached documents older than *max_age_hours*.  Returns count removed."""
    return _cleanup_cache_dir(get_document_cache_dir(), max_age_hours)
