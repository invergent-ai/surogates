from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from surogates.scheduled.schedule import (
    DYNAMIC_LOOP_MAX_DELAY_SECONDS,
    DYNAMIC_LOOP_MIN_DELAY_SECONDS,
    clamp_dynamic_loop_delay,
)
from surogates.scheduled.store import ScheduledSessionStore
from surogates.tools.registry import ToolRegistry, ToolSchema


_LOOP_WAIT_SCHEMA = ToolSchema(
    name="loop_wait",
    description=(
        "Set when the current dynamic /loop should run again, or declare "
        "the loop finished. Use only in dynamic loop sessions after deciding "
        "the next wait based on what you observed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "delay_seconds": {
                "type": "integer",
                "description": (
                    "Seconds to wait before the next loop iteration. Values "
                    "are clamped to 60 through 3600 seconds. Ignored when "
                    "``completed`` is true."
                ),
                "minimum": DYNAMIC_LOOP_MIN_DELAY_SECONDS,
                "maximum": DYNAMIC_LOOP_MAX_DELAY_SECONDS,
            },
            "reason": {
                "type": "string",
                "description": (
                    "Brief reason for the selected delay, or for finishing "
                    "the loop when ``completed`` is true."
                ),
            },
            "completed": {
                "type": "boolean",
                "description": (
                    "Set to true to mark the dynamic loop finished. The "
                    "schedule status flips to ``completed`` and no further "
                    "runs are scheduled. Use this when the loop's task is "
                    "done and there is no future work to wait for."
                ),
                "default": False,
            },
        },
        "required": ["delay_seconds", "reason"],
    },
)


_LOOP_COMPLETE_SCHEMA = ToolSchema(
    name="loop_complete",
    description=(
        "Mark the current ``/loop`` schedule finished. Call this from "
        "inside a fixed-cron ``/loop`` run when the prompt's stop "
        "condition is met (e.g. 'stop after 5 entries' and 5 entries "
        "now exist). The schedule's status flips to ``completed`` and "
        "no further runs are scheduled. For dynamic loops, use "
        "``loop_wait`` with ``completed: true`` instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Brief reason for completing the loop — typically the "
                    "stop condition that was reached."
                ),
            },
        },
        "required": ["reason"],
    },
)


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="loop_wait",
        schema=_LOOP_WAIT_SCHEMA,
        handler=_loop_wait_handler,
        toolset="scheduling",
    )
    registry.register(
        name="loop_complete",
        schema=_LOOP_COMPLETE_SCHEMA,
        handler=_loop_complete_handler,
        toolset="scheduling",
    )


async def _loop_wait_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    tenant = kwargs.get("tenant")
    if tenant is None or (
        getattr(tenant, "user_id", None) is None
        and getattr(tenant, "service_account_id", None) is None
    ):
        return _json({
            "success": False,
            "error": "Dynamic loops require a user or service-account principal",
        })

    agent_id = str(kwargs.get("agent_id") or "")
    if not agent_id:
        return _json({"success": False, "error": "agent_id is required"})

    session_config = kwargs.get("session_config") or {}
    if not session_config.get("scheduled_dynamic_loop"):
        return _json({
            "success": False,
            "error": "loop_wait is available only inside a dynamic loop session",
        })

    try:
        schedule_id = UUID(str(session_config.get("scheduled_session_id") or ""))
    except ValueError:
        return _json({"success": False, "error": "Invalid dynamic loop id"})

    try:
        session_id = UUID(str(kwargs.get("session_id") or ""))
    except ValueError:
        return _json({"success": False, "error": "Invalid session id"})

    delay = clamp_dynamic_loop_delay(int(arguments.get("delay_seconds") or 0))
    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        return _json({"success": False, "error": "reason is required"})
    completed = bool(arguments.get("completed") or False)

    store = kwargs.get("scheduled_store")
    if store is None:
        store = ScheduledSessionStore(kwargs["session_factory"])

    updated = await store.mark_dynamic_run_finished(
        schedule_id=schedule_id,
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        service_account_id=tenant.service_account_id,
        agent_id=agent_id,
        session_id=session_id,
        delay_seconds=delay,
        reason=reason,
        completed=completed,
    )
    if not updated:
        return _json({
            "success": False,
            "error": "Dynamic loop was not found or this session cannot update it",
        })
    return _json({
        "success": True,
        "delay_seconds": delay,
        "reason": reason,
        "completed": completed,
    })


async def _loop_complete_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    tenant = kwargs.get("tenant")
    if tenant is None or (
        getattr(tenant, "user_id", None) is None
        and getattr(tenant, "service_account_id", None) is None
    ):
        return _json({
            "success": False,
            "error": (
                "/loop schedules require a user or service-account principal"
            ),
        })

    agent_id = str(kwargs.get("agent_id") or "")
    if not agent_id:
        return _json({"success": False, "error": "agent_id is required"})

    session_config = kwargs.get("session_config") or {}
    if not session_config.get("scheduled_session_id"):
        return _json({
            "success": False,
            "error": "loop_complete is available only inside a /loop run",
        })
    if session_config.get("scheduled_dynamic_loop"):
        return _json({
            "success": False,
            "error": (
                "Dynamic loops complete via loop_wait(completed=true); "
                "loop_complete is for fixed-cron /loop runs only"
            ),
        })

    try:
        schedule_id = UUID(str(session_config.get("scheduled_session_id") or ""))
    except ValueError:
        return _json({"success": False, "error": "Invalid schedule id"})

    try:
        session_id = UUID(str(kwargs.get("session_id") or ""))
    except ValueError:
        return _json({"success": False, "error": "Invalid session id"})

    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        return _json({"success": False, "error": "reason is required"})

    store = kwargs.get("scheduled_store")
    if store is None:
        store = ScheduledSessionStore(kwargs["session_factory"])

    updated = await store.mark_loop_completed(
        schedule_id=schedule_id,
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        service_account_id=tenant.service_account_id,
        agent_id=agent_id,
        session_id=session_id,
        reason=reason,
    )
    if not updated:
        return _json({
            "success": False,
            "error": "Schedule was not found or this session cannot update it",
        })
    return _json({
        "success": True,
        "reason": reason,
    })


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)
