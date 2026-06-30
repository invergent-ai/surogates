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
from pathlib import PurePosixPath
from typing import Any

from surogates.channels.credentials import resolve_channel_credentials
from surogates.session.attachment_ingest import (
    ingest_attachment_bytes,
    safe_display_name,
    workspace_root_id,
)

logger = logging.getLogger(__name__)

MAX_FETCH_BYTES = 20 * 1024 * 1024


class ChannelFileError(Exception):
    """Base class for channel-file fetch failures."""


class ChannelFileNotFound(ChannelFileError):
    """Slack returned no metadata for the file id."""


class ChannelFileForbidden(ChannelFileError):
    """The file was not shared in this session's channel (tenant isolation)."""


class ChannelFileTooLarge(ChannelFileError):
    """The file exceeds the per-file size cap."""


class ChannelFileUnavailable(ChannelFileError):
    """Credentials are missing or the download failed."""


def _shared_in_channel(file_meta: dict, channel_id: str) -> bool:
    """True if *channel_id* appears in the file's share lists."""
    if not channel_id:
        return False
    for key in ("channels", "groups", "ims"):
        if channel_id in (file_meta.get(key) or []):
            return True
    return False


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

    meta = await platform.fetch_file_meta(creds=creds, file_id=file_id)
    if not meta:
        raise ChannelFileNotFound(f"File {file_id} was not found.")

    if not _shared_in_channel(meta, channel_id):
        raise ChannelFileForbidden(
            f"File {file_id} was not shared in this channel."
        )

    size = meta.get("size")
    if isinstance(size, int) and size > max_bytes:
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
    out = await ingest_attachment_bytes(
        storage,
        session=session,
        root_id=workspace_root_id(session),
        bucket=bucket,
        path=f"uploads/slack/fetch/{file_id}-{safe_name}",
        filename=safe_name,
        mime_type=meta.get("mimetype") or "application/octet-stream",
        data=data,
    )
    if "image" in out:
        return {"kind": "image", **out["image"]}
    return {"kind": "attachment", **out["attachment"]}
