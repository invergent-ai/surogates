from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from surogates.scheduled.prompt_guard import (
    ScheduledPromptBlocked,
    validate_scheduled_prompt,
)
from surogates.scheduled.schedule import parse_schedule
from surogates.scheduled.store import ScheduledSessionStore
from surogates.tools.registry import ToolRegistry, ToolSchema


_CRON_CREATE_SCHEMA = ToolSchema(
    name="cron_create",
    description=(
        "Schedule a user-owned prompt or slash command to run later or on a "
        "recurring cron cadence."
    ),
    parameters={
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": "5-field cron expression, for example */10 * * * *",
            },
            "prompt": {"type": "string"},
            "recurring": {"type": "boolean", "default": True},
            "durable": {"type": "boolean", "default": False},
            "name": {"type": "string"},
            "timezone": {"type": "string", "default": "UTC"},
        },
        "required": ["cron", "prompt"],
    },
)

_CRON_DELETE_SCHEMA = ToolSchema(
    name="cron_delete",
    description="Cancel a user-owned scheduled prompt by id.",
    parameters={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
)

_CRON_LIST_SCHEMA = ToolSchema(
    name="cron_list",
    description="List active user-owned scheduled prompts for this agent.",
    parameters={"type": "object", "properties": {}},
)


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="cron_create",
        schema=_CRON_CREATE_SCHEMA,
        handler=_cron_create_handler,
        toolset="scheduling",
    )
    registry.register(
        name="cron_delete",
        schema=_CRON_DELETE_SCHEMA,
        handler=_cron_delete_handler,
        toolset="scheduling",
    )
    registry.register(
        name="cron_list",
        schema=_CRON_LIST_SCHEMA,
        handler=_cron_list_handler,
        toolset="scheduling",
    )


async def _cron_create_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    context = _tool_context(kwargs)
    if isinstance(context, str):
        return context

    prompt = str(arguments.get("prompt") or "").strip()
    cron = str(arguments.get("cron") or "").strip()
    timezone_name = str(arguments.get("timezone") or "UTC")
    recurring = bool(arguments.get("recurring", True))
    name = str(arguments.get("name") or "").strip() or _default_name(prompt)

    try:
        validate_scheduled_prompt(prompt, source="cron_create")
        schedule = parse_schedule(cron, timezone_name=timezone_name)
    except (ScheduledPromptBlocked, ValueError) as exc:
        return _json({"success": False, "error": str(exc)})

    created = await context.store.create(
        org_id=context.tenant.org_id,
        user_id=context.tenant.user_id,
        agent_id=context.agent_id,
        name=name,
        prompt=prompt,
        schedule=schedule,
        source="tool",
        created_from_session_id=_uuid_or_none(kwargs.get("session_id")),
        repeat_limit=None if recurring else 1,
    )
    return _json({
        "success": True,
        "schedule": _schedule_payload(created),
        "recurring": recurring,
        "durable": bool(arguments.get("durable", False)),
    })


async def _cron_delete_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    context = _tool_context(kwargs)
    if isinstance(context, str):
        return context

    try:
        schedule_id = UUID(str(arguments.get("id") or ""))
    except ValueError:
        return _json({"success": False, "error": "Invalid schedule id"})

    deleted = await context.store.delete(
        org_id=context.tenant.org_id,
        user_id=context.tenant.user_id,
        agent_id=context.agent_id,
        schedule_id=schedule_id,
    )
    return _json({"success": True, "deleted": deleted, "id": str(schedule_id)})


async def _cron_list_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    context = _tool_context(kwargs)
    if isinstance(context, str):
        return context

    rows = await context.store.list_for_user(
        org_id=context.tenant.org_id,
        user_id=context.tenant.user_id,
        agent_id=context.agent_id,
    )
    return _json({
        "success": True,
        "schedules": [_schedule_payload(row) for row in rows],
    })


class _Context:
    def __init__(self, *, tenant: Any, agent_id: str, store: Any) -> None:
        self.tenant = tenant
        self.agent_id = agent_id
        self.store = store


def _tool_context(kwargs: dict[str, Any]) -> _Context | str:
    tenant = kwargs.get("tenant")
    if tenant is None or getattr(tenant, "user_id", None) is None:
        return _json({"success": False, "error": "Cron schedules are user-owned only"})
    agent_id = str(kwargs.get("agent_id") or "")
    if not agent_id:
        return _json({"success": False, "error": "agent_id is required"})
    store = kwargs.get("scheduled_store")
    if store is None:
        store = ScheduledSessionStore(kwargs["session_factory"])
    return _Context(tenant=tenant, agent_id=agent_id, store=store)


def _schedule_payload(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "prompt": row.prompt,
        "schedule": getattr(row, "schedule_display", None),
        "next_run_at": getattr(row, "next_run_at", None),
        "status": row.status,
    }


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _default_name(prompt: str) -> str:
    return f"Scheduled: {prompt[:60]}" if prompt else "Scheduled prompt"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)
