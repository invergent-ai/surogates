"""On-demand fetch of a file shared earlier in a channel.

Resolves the channel bot token from the vault, reads Slack ``files.info``
metadata, enforces that the file was shared in the session's own channel
(tenant isolation), downloads it with the hardened platform downloader, and
ingests the bytes into the session workspace via the shared attachment
pipeline.  Dependencies (platform, vault, storage) are injected so the
security-critical path is unit-testable without Slack or S3.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from typing import Any

from surogates.channels.credentials import resolve_channel_credentials
from surogates.channels.errors import ChannelApiError
from surogates.session.attachment_ingest import (
    ingest_attachment_bytes,
    is_raster_image_mime,
    safe_display_name,
    workspace_root_id,
)

logger = logging.getLogger(__name__)

MAX_FETCH_BYTES = 20 * 1024 * 1024

_SLACK_FILE_ID = re.compile(r"F[A-Z0-9]{6,}\Z")


class ChannelFileError(Exception):
    """Base class for channel-file fetch failures."""


class ChannelFileNotFound(ChannelFileError):
    """Slack returned no metadata for the file id."""


class ChannelFileForbidden(ChannelFileError):
    """The file was not shared in this session's channel (tenant isolation)."""


class ChannelFileTooLarge(ChannelFileError):
    """The file exceeds the per-file size cap."""


class ChannelFileRateLimited(ChannelFileError):
    """The channel platform is rate-limiting; the agent should retry shortly."""


class ChannelFileUnavailable(ChannelFileError):
    """Credentials are missing or the download failed."""


def _shared_in_channel(file_meta: dict, channel_id: str) -> bool:
    """True if *channel_id* appears in the file's share lists.

    Checks both the legacy top-level arrays (``channels``/``groups``/``ims``)
    and the modern ``shares`` object (``{"public": {"C123": [...]},
    "private": {"G123": [...]}}``) — files uploaded via the v2 API may record
    channel membership only under ``shares``, so consulting it too keeps a
    legitimately-shared private-channel file from being wrongly refused.
    """
    if not channel_id:
        return False
    for key in ("channels", "groups", "ims"):
        if channel_id in (file_meta.get(key) or []):
            return True
    for visibility in (file_meta.get("shares") or {}).values():
        if isinstance(visibility, dict) and channel_id in visibility:
            return True
    return False


async def _resolve_file_id(platform: Any, creds: dict, channel_id: str, ref: str) -> str:
    """Return *ref* if it is already a Slack file id, else resolve it as a
    filename against the channel's files (newest match wins). Raises
    ChannelFileNotFound listing available names when nothing matches."""
    ref = (ref or "").strip()
    if _SLACK_FILE_ID.fullmatch(ref):
        return ref
    lister = getattr(platform, "list_channel_files", None)
    try:
        files = await lister(creds=creds, channel_id=channel_id) if lister else []
    except ChannelApiError as exc:
        if exc.reason == "forbidden":
            raise ChannelFileForbidden("Access to this channel's files was denied.")
        if exc.reason == "rate_limited":
            raise ChannelFileRateLimited(
                "Slack is rate-limiting channel file listing; try again shortly."
            )
        raise ChannelFileUnavailable("Could not list this channel's files.")

    def _created(f: dict) -> float:
        try:
            return float(f.get("created") or f.get("timestamp") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _named(f: dict) -> tuple[str, str]:
        return (f.get("name") or ""), (f.get("title") or "")

    low = ref.lower()
    exact = [f for f in files if ref in _named(f)]
    ci = exact or [f for f in files if low in (n.lower() for n in _named(f))]
    base = PurePosixPath(ref).name.lower()
    bybase = ci or [f for f in files if PurePosixPath((f.get("name") or "")).name.lower() == base]
    if bybase:
        return max(bybase, key=_created).get("id") or ""
    names = sorted({(f.get("name") or "") for f in files if f.get("name")})
    hint = ("; files here: " + ", ".join(names[:10])) if names else ""
    raise ChannelFileNotFound(f"No file named '{ref}' in this channel{hint}.")


async def fetch_channel_file(
    *,
    platform: Any,
    vault: Any,
    storage: Any,
    session: Any,
    bucket: str,
    file_id: str,
    max_bytes: int = MAX_FETCH_BYTES,
) -> dict:
    """Resolve, validate, download and ingest a Slack channel file.

    Raises a :class:`ChannelFileError` subclass on any failure.  On success
    returns ``{"kind": "attachment"|"image", **entry}`` where ``entry`` is the
    ingested attachment/image record (carrying a workspace ``path``).
    """
    cfg = getattr(session, "config", None) or {}
    identifier = cfg.get("channel_identifier") or ""
    channel_id = cfg.get("slack_channel_id") or ""

    refs = platform.descriptor.vault_refs(identifier)
    creds = await resolve_channel_credentials(
        vault=vault, kind="slack", identifier=identifier,
        org_id=str(session.org_id), refs=refs,
    )
    if not (creds or {}).get("bot_token"):
        raise ChannelFileUnavailable("No Slack bot token for this channel.")

    file_id = await _resolve_file_id(platform, creds, channel_id, file_id)

    try:
        meta = await platform.fetch_file_meta(creds=creds, file_id=file_id)
    except ChannelApiError as exc:
        if exc.reason == "forbidden":
            raise ChannelFileForbidden(f"Access to file {file_id} was denied.")
        if exc.reason == "rate_limited":
            raise ChannelFileRateLimited(
                f"Slack is rate-limiting access to file {file_id}; "
                "try again shortly."
            )
        raise ChannelFileUnavailable(f"Could not read file {file_id}.")
    if not meta:
        raise ChannelFileNotFound(f"File {file_id} was not found.")

    if not _shared_in_channel(meta, channel_id):
        raise ChannelFileForbidden(
            f"File {file_id} was not shared in this channel."
        )

    size = meta.get("size")
    if isinstance(size, (int, float)) and size > max_bytes:
        raise ChannelFileTooLarge(
            f"File {file_id} exceeds the {max_bytes}-byte limit."
        )

    url = meta.get("url_private_download") or meta.get("url_private") or ""
    data = await platform.download_file(
        creds=creds, url=url, max_bytes=max_bytes,
    )
    if data is None:
        raise ChannelFileUnavailable(f"Could not download file {file_id}.")

    raw_name = meta.get("name") or file_id
    safe_name = safe_display_name(PurePosixPath(raw_name).name or file_id)
    safe_file_id = re.sub(r"[^\w.-]", "_", file_id) or "file"
    mime_type = meta.get("mimetype") or "application/octet-stream"
    out = await ingest_attachment_bytes(
        storage,
        session=session,
        root_id=workspace_root_id(session),
        bucket=bucket,
        path=f"uploads/slack/fetch/{safe_file_id}-{safe_name}",
        filename=safe_name,
        mime_type=mime_type,
        data=data,
        inline_images=False,
    )
    # Images also return an attachment entry (with path, no base64). Only
    # natively-renderable raster images get kind="image"; SVG/TIFF/BMP stay
    # "attachment" so the kind signal matches what the agent can actually view.
    entry = out["attachment"]
    if is_raster_image_mime(mime_type):
        return {"kind": "image", **entry}
    return {"kind": "attachment", **entry}
