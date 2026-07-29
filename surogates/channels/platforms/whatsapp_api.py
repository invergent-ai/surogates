"""WhatsApp Business Cloud API (Meta Graph) transport.

The only module in the WhatsApp platform that performs HTTP.  Every
function takes an ``httpx.AsyncClient`` and the per-tenant credentials
explicitly — nothing is held on the module or on an instance, because one
channels process serves every tenant.

All functions return ``(value, error)`` tuples rather than raising: the
platform layer's contract with the dispatcher is that a transport failure
degrades to a ``SendResult``/``None``, never an exception.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from typing import Any

import httpx

__all__ = [
    "DEFAULT_API_VERSION",
    "GRAPH_API_BASE",
    "MEDIA_SIZE_LIMITS",
    "MIME_EXTENSION_OVERRIDES",
    "download_media",
    "ext_for_mime",
    "format_graph_error",
    "graph_url",
    "send_message",
    "upload_media",
]

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"

#: Meta removes v20.0 on 2026-09-24; never pin below v23.0.
DEFAULT_API_VERSION = "v23.0"

#: Per-kind upload caps, enforced client-side before the round trip.
MEDIA_SIZE_LIMITS: dict[str, int] = {
    "image": 5 * 1024 * 1024,
    "video": 16 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "document": 100 * 1024 * 1024,
    "sticker": 100 * 1024,
}

#: ``mimetypes`` guesses badly for the formats WhatsApp actually sends.
MIME_EXTENSION_OVERRIDES: dict[str, str] = {
    "audio/ogg": ".ogg",          # not mimetypes' .oga
    "audio/x-opus+ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mp4": ".m4a",          # iOS voice memos
    "audio/x-m4a": ".m4a",
    "image/jpeg": ".jpg",         # not legacy .jpe
}

#: Meta media ids are plain opaque tokens.  Anything else is refused before
#: it can reach a URL path component.
_MEDIA_ID_RE = re.compile(r"[A-Za-z0-9_:-]+")

_UPLOAD_TIMEOUT = httpx.Timeout(120.0)


def graph_url(
    phone_number_id: str, path: str, *, api_version: str = DEFAULT_API_VERSION,
) -> str:
    """Return the phone-number-scoped Graph URL for *path*."""
    return f"{GRAPH_API_BASE}/{api_version}/{phone_number_id}/{path.lstrip('/')}"


def format_graph_error(status_code: int, body: dict) -> str:
    """Render a Graph error into the string the outbox classifier matches.

    With a code: ``graph error {code} (HTTP {status}): {message}``.  Without
    one: ``HTTP {status}: {message}`` — deliberately missing the ``graph
    error`` prefix, so a code-less error can never match a permanent-error
    prefix in ``delivery._PERMANENT_DELIVERY_ERRORS`` and stays retryable by
    construction.
    """
    err = (body or {}).get("error") or {}
    message = err.get("message") or "unknown error"
    code = err.get("code")
    if code is None:
        return f"HTTP {status_code}: {message}"
    return f"graph error {code} (HTTP {status_code}): {message}"


def ext_for_mime(mime: str) -> str:
    """Return a file extension for *mime*, preferring the override table."""
    base = (mime or "").split(";")[0].strip().lower()
    override = MIME_EXTENSION_OVERRIDES.get(base)
    if override:
        return override
    return mimetypes.guess_extension(base) or ".bin"


def _kind_for_mime(mime: str) -> str:
    """Map a MIME type to a WhatsApp media kind for cap lookup."""
    base = (mime or "").split("/")[0].strip().lower()
    if base in ("image", "video", "audio"):
        return base
    return "document"


async def send_message(
    client: httpx.AsyncClient,
    *,
    token: str,
    phone_number_id: str,
    payload: dict[str, Any],
    api_version: str = DEFAULT_API_VERSION,
) -> tuple[str | None, str | None]:
    """POST *payload* to ``/messages``.  Returns ``(wamid, error)``."""
    url = graph_url(phone_number_id, "messages", api_version=api_version)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = await client.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[whatsapp] /messages request failed (%s)", exc)
        return None, f"request failed: {exc}"

    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        data = {}

    if not response.is_success:
        return None, format_graph_error(response.status_code, data)

    messages = data.get("messages") or []
    wamid = messages[0].get("id") if messages else None
    return wamid, None


async def upload_media(
    client: httpx.AsyncClient,
    *,
    token: str,
    phone_number_id: str,
    filename: str,
    data: bytes,
    mime_type: str,
    api_version: str = DEFAULT_API_VERSION,
) -> tuple[str | None, str | None]:
    """Upload *data* to ``/media``.  Returns ``(media_id, error)``.

    The size cap is checked client-side before the round trip, with the cap
    value in the error string so an operator can see what was exceeded.
    """
    kind = _kind_for_mime(mime_type)
    cap = MEDIA_SIZE_LIMITS.get(kind, MEDIA_SIZE_LIMITS["document"])
    if len(data) > cap:
        return None, (
            f"File {filename} is {len(data)} bytes; "
            f"Cloud API {kind} cap is {cap} bytes"
        )

    url = graph_url(phone_number_id, "media", api_version=api_version)
    # No Content-Type header — httpx sets the multipart boundary itself.
    headers = {"Authorization": f"Bearer {token}"}
    files = {
        "file": (filename, data, mime_type),
        "messaging_product": (None, "whatsapp"),
        "type": (None, mime_type),
    }
    try:
        response = await client.post(
            url, files=files, headers=headers, timeout=_UPLOAD_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[whatsapp] /media upload failed for %s (%s)", filename, exc)
        return None, f"upload failed: {exc}"

    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {}

    if not response.is_success:
        return None, format_graph_error(response.status_code, body)
    return body.get("id"), None


async def download_media(
    client: httpx.AsyncClient,
    *,
    token: str,
    media_id: str,
    max_bytes: int,
    api_version: str = DEFAULT_API_VERSION,
) -> tuple[bytes | None, str | None]:
    """Fetch inbound media by id.  Returns ``(data, mime_type)``.

    Two hops: ``GET /{media_id}`` yields a signed lookaside URL that expires
    in about five minutes, then a second GET fetches the bytes.  The signed
    URL **still requires** the bearer token — Meta documents this and it is
    the most common inbound-media mistake.

    ``max_bytes`` is checked against the metadata's ``file_size`` before the
    body is fetched.  On a 403/410 the metadata hop is retried once, since
    the URL may simply have expired.  Any failure returns ``(None, None)``
    so a media problem never kills the surrounding event.
    """
    # The id becomes a URL path component; refuse anything path-shaped
    # before any HTTP.  Meta media ids are plain opaque tokens.
    if not _MEDIA_ID_RE.fullmatch(media_id or ""):
        logger.warning("[whatsapp] refusing path-shaped media id %r", media_id)
        return None, None

    headers = {"Authorization": f"Bearer {token}"}
    meta_url = f"{GRAPH_API_BASE}/{api_version}/{media_id}"

    for attempt in (1, 2):
        try:
            meta_response = await client.get(meta_url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[whatsapp] media metadata request failed (%s)", exc)
            return None, None
        if not meta_response.is_success:
            logger.warning(
                "[whatsapp] media metadata HTTP %s for %s",
                meta_response.status_code, media_id,
            )
            return None, None

        try:
            meta = meta_response.json()
        except Exception:  # noqa: BLE001
            return None, None

        blob_url = meta.get("url")
        mime_type = meta.get("mime_type") or ""
        if not blob_url:
            return None, None

        file_size = meta.get("file_size")
        if isinstance(file_size, int) and file_size > max_bytes:
            logger.warning(
                "[whatsapp] media %s is %d bytes, over the %d cap — skipping",
                media_id, file_size, max_bytes,
            )
            return None, None

        try:
            blob_response = await client.get(blob_url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[whatsapp] media body request failed (%s)", exc)
            return None, None

        if blob_response.status_code in (403, 410) and attempt == 1:
            # Signed URL expired between the two hops — re-resolve it once.
            continue
        if not blob_response.is_success:
            logger.warning(
                "[whatsapp] media body HTTP %s for %s",
                blob_response.status_code, media_id,
            )
            return None, None

        data = blob_response.content
        if len(data) > max_bytes:
            logger.warning(
                "[whatsapp] media %s body exceeded the %d cap — skipping",
                media_id, max_bytes,
            )
            return None, None
        return data, mime_type

    return None, None
