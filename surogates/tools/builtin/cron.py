from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.scheduled.prompt_guard import (
    ScheduledPromptBlocked,
    validate_scheduled_prompt,
)
from surogates.scheduled.schedule import local_iso, parse_schedule
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema


_CRON_CREATE_SCHEMA = ToolSchema(
    name="cron_create",
    description=(
        "Schedule a prompt to run later or on a recurring cron cadence, as a "
        "fresh session whose answer appears in this chat. Turn the user's "
        "request, in whatever language, into a 5-field cron and an IANA "
        "timezone. Nothing is made until the user has confirmed it in this "
        "turn by answering ask_user_question: until then this returns what it "
        "would make, with its next run on the schedule's own clock, and makes "
        "nothing. Ask them in their language, giving the cadence in plain "
        "words, the timezone, the next run, a one-line summary of the prompt, "
        "and that it runs until they cancel it (once, when recurring is "
        "false); then call again with the same arguments."
    ),
    parameters={
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": "5-field cron expression, for example 30 8 * * 1-5 (08:30 Monday to Friday)",
            },
            "prompt": {
                "type": "string",
                "description": "Self-contained instructions for the run, which cannot see this conversation.",
            },
            "recurring": {"type": "boolean", "default": True},
            "durable": {"type": "boolean", "default": False},
            "name": {"type": "string"},
            "timezone": {
                "type": "string",
                "default": "UTC",
                "description": "IANA timezone the cron is read in, for example Europe/Bucharest",
            },
        },
        "required": ["cron", "prompt"],
    },
)

_CRON_DELETE_SCHEMA = ToolSchema(
    name="cron_delete",
    description="Cancel one of the user's scheduled prompts by id, when they ask to stop or change it.",
    parameters={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
)

_CRON_LIST_SCHEMA = ToolSchema(
    name="cron_list",
    description="List the user's active scheduled prompts for this agent.",
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
    context = await _tool_context(kwargs)
    if isinstance(context, str):
        return context

    prompt = str(arguments.get("prompt") or "").strip()
    cron = str(arguments.get("cron") or "").strip()
    timezone_name = str(arguments.get("timezone") or "UTC")
    recurring = bool(arguments.get("recurring", True))
    # The model names it; a project's Routines tab shows at most 500 units.
    name = (str(arguments.get("name") or "").strip() or _default_name(prompt))[:200]

    try:
        validate_scheduled_prompt(prompt, source="cron_create")
        schedule = parse_schedule(cron, timezone_name=timezone_name)
    except (ScheduledPromptBlocked, ValueError) as exc:
        return _json({"success": False, "error": str(exc)})

    if not await _answered_this_turn(context.session_store, context.session_id):
        return _json({
            "success": False,
            "error": (
                "Not made yet. Confirm it with the user by ask_user_question, in their "
                "language, then call cron_create again with the same arguments."
            ),
            "would_create": {
                "cron": schedule.cron,
                "timezone": schedule.timezone_name,
                "next_run_at": local_iso(schedule.next_after(datetime.now(timezone.utc)), timezone_name),
                "runs": "until cancelled" if recurring else "once",
            },
        })

    created = await context.store.create(
        org_id=context.org_id,
        user_id=context.principal.user_id,
        service_account_id=context.principal.service_account_id,
        agent_id=context.agent_id,
        name=name,
        prompt=prompt,
        schedule=schedule,
        source="tool",
        created_from_session_id=context.session_id,
        repeat_limit=None if recurring else 1,
    )
    return _json({
        "success": True,
        "schedule": _schedule_payload(created),
        "recurring": recurring,
        "durable": bool(arguments.get("durable", False)),
    })


async def _cron_delete_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    context = await _tool_context(kwargs)
    if isinstance(context, str):
        return context

    try:
        schedule_id = UUID(str(arguments.get("id") or ""))
    except ValueError:
        return _json({"success": False, "error": "Invalid schedule id"})

    deleted = await context.store.delete(
        org_id=context.org_id,
        user_id=context.principal.user_id,
        service_account_id=context.principal.service_account_id,
        agent_id=context.agent_id,
        schedule_id=schedule_id,
    )
    return _json({"success": True, "deleted": deleted, "id": str(schedule_id)})


async def _cron_list_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    context = await _tool_context(kwargs)
    if isinstance(context, str):
        return context

    rows = await context.store.list_for_user(
        org_id=context.org_id,
        user_id=context.principal.user_id,
        service_account_id=context.principal.service_account_id,
        agent_id=context.agent_id,
    )
    return _json({
        "success": True,
        "schedules": [_schedule_payload(row) for row in rows],
    })


class _Context:
    def __init__(
        self, *, org_id: Any, principal: Any, agent_id: str, session_id: UUID,
        session_store: Any, store: Any,
    ) -> None:
        self.org_id = org_id
        self.principal = principal
        self.agent_id = agent_id
        self.session_id = session_id
        self.session_store = session_store
        self.store = store


async def _tool_context(kwargs: dict[str, Any]) -> _Context | str:
    tenant = kwargs.get("tenant")
    session_store = kwargs.get("session_store")
    session_id = _uuid_or_none(kwargs.get("session_id"))
    if tenant is None or session_store is None or session_id is None:
        return _json({"success": False, "error": "Schedules are made from a session"})
    agent_id = str(kwargs.get("agent_id") or "")
    if not agent_id:
        return _json({"success": False, "error": "agent_id is required"})
    # The schedule is its sender's, as a /loop is: not ``tenant``, which on a
    # managed channel is the agent's own service account.
    principal = await session_store.resolve_acting_principal(session_id)
    if principal.user_id is None and principal.service_account_id is None:
        return _json({
            "success": False,
            "error": "Schedules need a signed-in user or a service account; this session has neither.",
        })
    store = kwargs.get("scheduled_store")
    if store is None:
        store = ScheduledSessionStore(kwargs["session_factory"])
    return _Context(
        org_id=tenant.org_id, principal=principal, agent_id=agent_id,
        session_id=session_id, session_store=session_store, store=store,
    )


async def _answered_this_turn(session_store: Any, session_id: UUID) -> bool:
    """Whether the user answered an ``ask_user_question`` since their last
    message: someone was there, and was asked, before something that
    outlives the chat is made.  What they were asked is the model's part."""
    said = await session_store.get_events(
        session_id,
        types=[EventType.USER_MESSAGE, EventType.ASK_USER_QUESTION_RESPONSE],
    )
    return bool(said) and said[-1].type == EventType.ASK_USER_QUESTION_RESPONSE.value


def _schedule_payload(row: Any) -> dict[str, Any]:
    timezone_name = getattr(row, "timezone", None) or "UTC"
    return {
        "id": str(row.id),
        "name": row.name,
        "prompt": row.prompt,
        "schedule": getattr(row, "schedule_display", None),
        "timezone": timezone_name,
        "next_run_at": local_iso(getattr(row, "next_run_at", None), timezone_name),
        "expires_at": local_iso(getattr(row, "expires_at", None), timezone_name),
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
