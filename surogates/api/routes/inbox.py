"""HTTP routes for the agent inbox."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from surogates.config import enqueue_session
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.session.events import EventType
from surogates.session.inbox_payload import ACKNOWLEDGE_ONLY_KINDS
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.ask_user_question import (
    ASK_USER_QUESTION_MAX_WAIT_SECONDS,
)

router = APIRouter(prefix="/inbox")

# The inbox belongs to one person AND one agent. A person talking to
# several agents accumulates items from all of them on the same principal
# — the SPA is served per agent, so showing it the others' work is a leak
# of the same kind the session list already refuses to make. Resolved the
# way every other agent-scoped route resolves it: host subdomain in
# production, explicit agent_id otherwise.
AgentRuntime = Annotated[AgentRuntimeContext, Depends(agent_runtime_context_dep)]

# Repeat the parameter to ask for several at once. Declared as a literal
# so an unknown value is rejected by the framework, rather than filtering
# everything out and reading as an empty inbox.
InboxStatus = Literal["pending", "acknowledged", "responded", "expired"]


class InboxResponse(BaseModel):
    decision: str | None = Field(default=None, pattern="^(approve|reject)$")
    completed: bool | None = None


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


async def _resolve_agent_fields(
    request: Request, session_ids: list[UUID]
) -> dict:
    """Map session_id -> {"agent_id", "agent_slug"} for serialization.

    `agent_slug` is best-effort: an absent cache or a cache miss (LookupError)
    leaves it None so the item still serializes (the web then falls back to
    in-place). Other errors propagate, matching the cache's other callers, so
    a real management-plane failure is not silently masked. The distinct
    per-owner-agent lookups run concurrently.
    """
    store = request.app.state.session_store
    agent_by_session = await store.get_agent_ids_for_sessions(session_ids)
    cache = getattr(request.app.state, "runtime_config_cache", None)
    distinct_agents = list(dict.fromkeys(agent_by_session.values()))

    async def _slug(agent_id: str) -> str | None:
        if cache is None:
            return None
        try:
            payload = await cache.get(agent_id)
        except LookupError:
            return None
        return payload.get("slug")

    slugs = await asyncio.gather(*(_slug(agent_id) for agent_id in distinct_agents))
    slug_by_agent = dict(zip(distinct_agents, slugs))
    return {
        sid: {"agent_id": agent_id, "agent_slug": slug_by_agent.get(agent_id)}
        for sid, agent_id in agent_by_session.items()
    }


async def _agent_fields_for(request: Request, session_id: UUID) -> dict:
    """Agent fields for one inbox item's session (single-item serialize path)."""
    fields = await _resolve_agent_fields(request, [session_id])
    return fields.get(session_id, {})


def _expires_at(item) -> str | None:
    """When a question stops being answerable, or None if it does not.

    Only a question has a deadline: it is answerable while the tool call
    that asked it is parked waiting, and that wait is capped. Computed
    here so the one place that knows the cap is the one that owns it —
    every client used to mirror the constant, and a mirror is only
    correct until someone changes the original.
    """
    if item.kind != "input_required":
        return None
    deadline = item.created_at + timedelta(
        seconds=ASK_USER_QUESTION_MAX_WAIT_SECONDS
    )
    return deadline.isoformat()


def _serialize_item(item, agent_fields: dict | None = None) -> dict:
    fields = agent_fields or {}
    return {
        "id": item.id,
        "org_id": str(item.org_id),
        # Null for service-account-owned items; str() would emit "None".
        "user_id": str(item.user_id) if item.user_id is not None else None,
        "session_id": str(item.session_id),
        "source_event_id": item.source_event_id,
        "kind": item.kind,
        "status": item.status,
        "title": item.title,
        "body": item.body,
        "payload": item.payload,
        "action_ref": item.action_ref,
        "created_at": item.created_at.isoformat(),
        "expires_at": _expires_at(item),
        "updated_at": item.updated_at.isoformat(),
        "read_at": item.read_at.isoformat() if item.read_at else None,
        "responded_at": item.responded_at.isoformat()
        if item.responded_at
        else None,
        "agent_id": fields.get("agent_id"),
        "agent_slug": fields.get("agent_slug"),
    }


async def _wake_session_from_request(request: Request, session_id: UUID) -> None:
    session = await request.app.state.session_store.get_session(session_id)
    await enqueue_session(
        request.app.state.redis,
        org_id=str(session.org_id),
        agent_id=session.agent_id,
        session_id=session_id,
    )


@router.get("")
async def list_inbox(
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    agent_runtime: AgentRuntime,
    status: list[InboxStatus] | None = Query(default=None),
    kind: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    items = await store.list_inbox(
        user_id=tenant.user_id,
        agent_id=agent_runtime.agent_id,
        status=status or [],
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
    agent_fields = await _resolve_agent_fields(
        request, [item.session_id for item in items]
    )
    return {
        "items": [
            _serialize_item(item, agent_fields.get(item.session_id))
            for item in items
        ],
        "next_cursor": next_cursor,
    }


@router.get("/stream")
async def stream_inbox(
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    agent_runtime: AgentRuntime,
):
    tenant = _require_user_tenant(tenant)
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis is required for inbox streaming.",
        )
    channel = f"surogates:inbox:{tenant.user_id}"

    async def event_gen():
        store = request.app.state.session_store
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            # Pending only, matching what the badge counts from the list
            # endpoint: an unread item the user already acknowledged or
            # responded to is history, and counting it here made the
            # badge jump the moment the stream connected.
            snapshot = await asyncio.shield(
                store.list_inbox(
                    user_id=tenant.user_id,
                    agent_id=agent_runtime.agent_id,
                    status="pending",
                    limit=200,
                )
            )
            unread_ids = [item.id for item in snapshot if item.read_at is None]
            yield {
                "event": "snapshot",
                "data": json.dumps({"unread_ids": unread_ids}, default=str),
            }

            while True:
                if await request.is_disconnected():
                    return
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None:
                    continue
                raw = message.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if not raw:
                    continue
                try:
                    item_id, kind = str(raw).split(":", 1)
                    data = {"item_id": int(item_id), "kind": kind}
                except (TypeError, ValueError):
                    continue
                # The channel is keyed by principal, so every agent this
                # person talks to publishes onto it. Nudging about someone
                # else's agent would send the client after an item it is
                # then refused.
                if await store.get_inbox_item(
                    item_id=data["item_id"],
                    user_id=tenant.user_id,
                    agent_id=agent_runtime.agent_id,
                ) is None:
                    continue
                yield {"event": "item", "data": json.dumps(data, default=str)}
        except asyncio.CancelledError:
            return
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass

    return EventSourceResponse(event_gen())


@router.get("/{item_id}")
async def get_inbox_item(
    item_id: int,
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    agent_runtime: AgentRuntime,
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    item = await store.get_inbox_item(
        item_id=item_id,
        user_id=tenant.user_id,
        agent_id=agent_runtime.agent_id,
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbox item not found.",
        )
    return _serialize_item(item, await _agent_fields_for(request, item.session_id))


@router.post("/{item_id}/read")
async def mark_inbox_item_read(
    item_id: int,
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    agent_runtime: AgentRuntime,
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    item = await store.get_inbox_item(
        item_id=item_id,
        user_id=tenant.user_id,
        agent_id=agent_runtime.agent_id,
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbox item not found.",
        )
    item = await store.mark_inbox_read(
        item_id=item_id,
        user_id=tenant.user_id,
        agent_id=agent_runtime.agent_id,
    )
    return _serialize_item(item, await _agent_fields_for(request, item.session_id))


@router.post("/{item_id}/ack")
async def acknowledge_inbox_item(
    item_id: int,
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    agent_runtime: AgentRuntime,
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    item = await store.get_inbox_item(
        item_id=item_id,
        user_id=tenant.user_id,
        agent_id=agent_runtime.agent_id,
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbox item not found.",
        )
    if item.kind not in ACKNOWLEDGE_ONLY_KINDS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Inbox item kind is not acknowledgeable.",
        )
    try:
        item = await store.set_inbox_status(
            item_id=item_id,
            user_id=tenant.user_id,
            agent_id=agent_runtime.agent_id,
            new_status="acknowledged",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return _serialize_item(item, await _agent_fields_for(request, item.session_id))


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_inbox_item(
    item_id: int,
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    agent_runtime: AgentRuntime,
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    item = await store.delete_inbox_item(
        item_id=item_id,
        user_id=tenant.user_id,
        agent_id=agent_runtime.agent_id,
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbox item not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{item_id}/respond")
async def respond_to_inbox_item(
    item_id: int,
    payload: InboxResponse,
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    agent_runtime: AgentRuntime,
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    item = await store.get_inbox_item(
        item_id=item_id,
        user_id=tenant.user_id,
        agent_id=agent_runtime.agent_id,
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inbox item not found.",
        )
    if item.kind not in {"governance_gate", "action_required"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Items of kind '{item.kind}' are not respondable here.",
        )

    if item.kind == "governance_gate":
        if payload.decision not in {"approve", "reject"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Governance response requires approve or reject.",
            )
        decision = payload.decision
        tool_name = item.payload.get("tool_name", "unknown")
        tool_call_id = item.payload.get("tool_call_id", "")
        user_message = (
            f"[governance decision] {decision.upper()} for {tool_name}"
            f" (call {tool_call_id})."
        )
        event_data = {
            "content": user_message,
            "source": "inbox_governance_decision",
            "decision": decision,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "inbox_item_id": item.id,
        }
        if decision == "approve":
            # The decision has to outlive this request or the retry is
            # blocked identically.  Key the grant on the ORIGINAL arguments:
            # the inbox payload carries only a truncated excerpt, and a
            # grant scoped to a truncation would match nothing.
            await store.grant_tool_call_approval(
                session_id=item.session_id,
                org_id=item.org_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                granted_by=tenant.user_id,
            )
    else:
        if payload.completed is not True:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Action-required response must set completed=true.",
            )
        action_type = item.payload.get("action_type", "manual")
        instructions = item.payload.get("instructions", "")
        user_message = (
            f"[user action completed] {action_type}. The user completed the "
            "requested action and the agent may continue."
        )
        event_data = {
            "content": user_message,
            "source": "inbox_action_completed",
            "action_type": action_type,
            "instructions": instructions,
            "inbox_item_id": item.id,
        }

    await store.emit_event(item.session_id, EventType.USER_MESSAGE, event_data)
    try:
        item = await store.set_inbox_status(
            item_id=item_id,
            user_id=tenant.user_id,
            agent_id=agent_runtime.agent_id,
            new_status="responded",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    await _wake_session_from_request(request, item.session_id)
    return _serialize_item(item, await _agent_fields_for(request, item.session_id))
