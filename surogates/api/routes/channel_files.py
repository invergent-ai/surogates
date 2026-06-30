"""Channel-file fetch REST API — session-scoped on-demand file pull.

A channel agent calls this (via ``fetch_channel_file`` over the harness API
client) to download a file shared in an earlier message of its own channel.
The credential vault and storage live here, so the bot token never leaves
the server.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

import surogates.channels.platforms  # noqa: F401  # ensure SlackPlatform self-registers in this process

from surogates.channels.file_fetch import (
    ChannelFileForbidden,
    ChannelFileNotFound,
    ChannelFileTooLarge,
    ChannelFileUnavailable,
    fetch_channel_file,
)
from surogates.channels.platform_resolve import effective_channel_platform
from surogates.channels.registry import registry
from surogates.session.store import SessionNotFoundError, SessionStore
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_session_store(request: Request) -> SessionStore:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


async def _resolve_session_bucket(
    store: SessionStore, session_id: UUID, tenant: TenantContext,
) -> tuple[Any, str]:
    try:
        session = await store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    if not tenant.owns_session(session.org_id, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found.",
        )
    bucket = session.config.get("storage_bucket")
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Session {session_id} has no agent bucket.",
        )
    return session, bucket


@router.post("/sessions/{session_id}/channel-files/{file_id}")
async def fetch_channel_file_route(
    session_id: UUID,
    file_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict:
    """Download a file shared in this session's channel into the workspace."""
    store = _get_session_store(request)
    session, bucket = await _resolve_session_bucket(store, session_id, tenant)

    # Ambient sessions carry a slack channel_id but channel="ambient".
    channel = effective_channel_platform(session)
    if channel != "slack":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Channel-file fetch is only supported for Slack sessions.",
        )

    platform = registry.get("slack")
    if platform is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Slack platform is not available.",
        )

    try:
        return await fetch_channel_file(
            platform=platform,
            vault=request.app.state.credential_vault,
            storage=request.app.state.storage,
            session=session,
            bucket=bucket,
            file_id=file_id,
        )
    except ChannelFileForbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except ChannelFileNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ChannelFileTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc),
        )
    except ChannelFileUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
