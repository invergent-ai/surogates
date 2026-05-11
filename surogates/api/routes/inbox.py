"""HTTP routes for the agent inbox."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter(prefix="/inbox")


def _require_user_tenant(tenant: TenantContext) -> TenantContext:
    if tenant.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inbox requires a user account.",
        )
    return tenant


def _encode_cursor(created_at: datetime, item_id: int) -> str:
    raw = json.dumps([created_at.isoformat(), item_id])
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        iso, item_id = json.loads(raw)
        return datetime.fromisoformat(iso), int(item_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor.",
        ) from exc


def _serialize_item(item) -> dict:
    return {
        "id": item.id,
        "org_id": str(item.org_id),
        "user_id": str(item.user_id),
        "session_id": str(item.session_id),
        "source_event_id": item.source_event_id,
        "kind": item.kind,
        "status": item.status,
        "title": item.title,
        "body": item.body,
        "payload": item.payload,
        "action_ref": item.action_ref,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "read_at": item.read_at.isoformat() if item.read_at else None,
        "responded_at": item.responded_at.isoformat()
        if item.responded_at
        else None,
    }


@router.get("")
async def list_inbox(
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    items = await store.list_inbox(
        user_id=tenant.user_id,
        status=status,
        kind=kind,
        session_id=UUID(session_id) if session_id else None,
        cursor=_decode_cursor(cursor),
        limit=limit,
    )
    next_cursor = (
        _encode_cursor(items[-1].created_at, items[-1].id)
        if len(items) == limit
        else None
    )
    return {
        "items": [_serialize_item(item) for item in items],
        "next_cursor": next_cursor,
    }
