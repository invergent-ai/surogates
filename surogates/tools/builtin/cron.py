from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.channels.constants import STUDIO_CHANNEL
from surogates.scheduled.prompt_guard import (
    ScheduledPromptBlocked,
    validate_scheduled_prompt,
)
from surogates.scheduled.schedule import (
    ParsedSchedule,
    local_iso,
    once_schedule,
    schedule_from_fields,
)
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session.events import EventType
from surogates.session.execution_context import capture_schedule_context
from surogates.session.interactive_input import match_choice_label
from surogates.tools.registry import ToolRegistry, ToolSchema

#: The channel of an operator's own chat with their agent.

_CADENCE_PROPERTIES: dict[str, Any] = {
    "cron": {
        "type": "string",
        "description": "5-field cron read on the timezone's clock, e.g. 30 8 * * 1-5 (08:30 Monday to Friday).",
    },
    "interval": {
        "type": "string",
        "description": "A fixed period from now, e.g. 90m, 2h, 3d, for cadences a cron cannot say.",
    },
    "at": {
        "type": "string",
        "description": "One run at this local date and time, e.g. 2026-10-28T15:30.",
    },
    "days": {
        "type": "array",
        "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        "description": "With times: the weekdays it runs on (every day when omitted).",
    },
    "times": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Local times of day, e.g. [\"09:00\", \"17:30\"]; each fires once a day across clock changes.",
    },
    "timezone": {
        "type": "string",
        "default": "UTC",
        "description": "IANA timezone the cadence is read in, e.g. Europe/Bucharest.",
    },
}

_ID = {"id": {"type": "string", "description": "The schedule id, from cron_list."}}

_CRON_CREATE_SCHEMA = ToolSchema(
    name="cron_create",
    description=(
        "Schedule a prompt to run later, once or on a cadence, as a fresh "
        "session whose answer appears in this chat. Give exactly one of "
        "cron, interval, at, or times (with days), plus an IANA timezone; "
        "turn the user's words, in any language, into these. A /loop "
        "message that reaches you is one the harness could not read: make "
        "it with this. Where you can ask the user, nothing is made until "
        "they approve it in this turn: until then this returns what it "
        "would make, with its next run on the schedule's own clock. Ask "
        "with ask_user_question, in their language, giving the cadence in "
        "plain words, the timezone, the next run, a one-line summary of "
        "the prompt and that it runs until cancelled (once, for a one-off), "
        "with the approving choice FIRST; then call again with the same "
        "arguments. Never say a schedule exists or runs unless a tool result "
        "shows it; cron_list shows what exists."
    ),
    parameters={
        "type": "object",
        "properties": {
            **_CADENCE_PROPERTIES,
            "prompt": {
                "type": "string",
                "description": "Self-contained instructions for the run, which cannot see this conversation.",
            },
            "recurring": {
                "type": "boolean",
                "default": True,
                "description": "false with a cron: run once, at its next match.",
            },
            "name": {"type": "string"},
        },
        "required": ["prompt"],
    },
)

_CRON_LIST_SCHEMA = ToolSchema(
    name="cron_list",
    description=(
        "List this agent's schedules you can see: cadence, timezone, status, "
        "next run, runs so far and the last error. Check here before saying "
        "what is scheduled."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "default": "active,paused",
                "description": "One of active, paused, completed, cancelled, failed, a comma list, or all.",
            },
        },
    },
)

_CRON_GET_SCHEMA = ToolSchema(
    name="cron_get",
    description="One schedule in full, with its last run and why it ended, if it did.",
    parameters={"type": "object", "properties": dict(_ID), "required": ["id"]},
)

_CRON_UPDATE_SCHEMA = ToolSchema(
    name="cron_update",
    description=(
        "Change a schedule's prompt, name, cadence or timezone. A new cadence "
        "is given as in cron_create (one of cron, interval, at, times). "
        "Confirm with the user first, as for cron_create. A scheduled run may "
        "change only its own schedule, without asking."
    ),
    parameters={
        "type": "object",
        "properties": {
            **_ID,
            **_CADENCE_PROPERTIES,
            "prompt": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["id"],
    },
)

_CRON_PAUSE_SCHEMA = ToolSchema(
    name="cron_pause",
    description="Pause a schedule: no runs until it is resumed.",
    parameters={"type": "object", "properties": dict(_ID), "required": ["id"]},
)

_CRON_RESUME_SCHEMA = ToolSchema(
    name="cron_resume",
    description="Resume a paused schedule from its next run after now.",
    parameters={"type": "object", "properties": dict(_ID), "required": ["id"]},
)

_CRON_RUN_NOW_SCHEMA = ToolSchema(
    name="cron_run_now",
    description="Start a run of an active schedule now, as well as its planned ones.",
    parameters={"type": "object", "properties": dict(_ID), "required": ["id"]},
)

_CRON_DELETE_SCHEMA = ToolSchema(
    name="cron_delete",
    description=(
        "Cancel a schedule for good, when the user asks to stop it. A run still going is stopped. "
        "A scheduled run ends its own schedule this way, with a reason, when its prompt says it is done."
    ),
    parameters={
        "type": "object",
        "properties": {**_ID, "reason": {"type": "string", "description": "Why a run ends its own schedule."}},
        "required": ["id"],
    },
)


def register(registry: ToolRegistry) -> None:
    for name, schema, handler in (
        ("cron_create", _CRON_CREATE_SCHEMA, _cron_create_handler),
        ("cron_list", _CRON_LIST_SCHEMA, _cron_list_handler),
        ("cron_get", _CRON_GET_SCHEMA, _cron_get_handler),
        ("cron_update", _CRON_UPDATE_SCHEMA, _cron_update_handler),
        ("cron_pause", _CRON_PAUSE_SCHEMA, _cron_pause_handler),
        ("cron_resume", _CRON_RESUME_SCHEMA, _cron_resume_handler),
        ("cron_run_now", _CRON_RUN_NOW_SCHEMA, _cron_run_now_handler),
        ("cron_delete", _CRON_DELETE_SCHEMA, _cron_delete_handler),
    ):
        registry.register(name=name, schema=schema, handler=handler, toolset="scheduling")


async def _cron_create_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    context = await _tool_context(kwargs)
    if isinstance(context, str):
        return context
    if refusal := _not_in_a_run(context):
        return refusal

    prompt = str(arguments.get("prompt") or "").strip()
    recurring = bool(arguments.get("recurring", True))
    # The model names it; a project's Routines tab shows at most 500 units.
    name = (str(arguments.get("name") or "").strip() or _default_name(prompt))[:200]

    try:
        validate_scheduled_prompt(prompt, source="cron_create")
        schedule = _cadence(arguments)
        if schedule is None:
            raise ValueError("Give one of cron, interval, at, or times.")
        if not recurring and schedule.kind != "once":
            first = schedule.anchored(_now()).next_after(_now())
            if first is None:
                raise ValueError("That schedule has no next run.")
            schedule = once_schedule(first, timezone_name=schedule.timezone_name)
        next_run = schedule.anchored(_now()).next_after(_now())
        if next_run is None:
            raise ValueError("That time has already passed.")
    except (ScheduledPromptBlocked, ValueError) as exc:
        return _json({"success": False, "error": str(exc)})

    refusal = await _confirmation_refusal(context, kwargs, would={
        "cadence": schedule.display,
        "timezone": schedule.timezone_name,
        "next_run_at": local_iso(next_run, schedule.timezone_name),
        "runs": "once" if schedule.kind == "once" else "until cancelled",
    })
    if refusal is not None:
        return refusal

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
        context=capture_schedule_context(kwargs.get("session_config"), kwargs.get("channel")),
    )
    return _json({"success": True, "schedule": _schedule_payload(created)})


async def _cron_list_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    context = await _tool_context(kwargs)
    if isinstance(context, str):
        return context
    rows = await context.store.list_for_user(
        org_id=context.org_id,
        user_id=context.principal.user_id,
        service_account_id=context.principal.service_account_id,
        agent_id=context.agent_id,
        status=str(arguments.get("status") or "active,paused"),
        limit=50,
    )
    return _json({"success": True, "schedules": [_schedule_payload(row) for row in rows]})


async def _cron_get_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    context = await _tool_context(kwargs)
    if isinstance(context, str):
        return context
    schedule_id = _schedule_id(arguments)
    if isinstance(schedule_id, str):
        return schedule_id
    row = await context.store.get_for(
        schedule_id, org_id=context.org_id, agent_id=context.agent_id, **context.owner,
    )
    if row is None:
        return _not_found(schedule_id)
    return _json({"success": True, "schedule": _schedule_payload(row, full=True)})


async def _cron_update_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    context = await _tool_context(kwargs)
    if isinstance(context, str):
        return context
    schedule_id = _schedule_id(arguments)
    if isinstance(schedule_id, str):
        return schedule_id
    if refusal := _not_its_own(context, schedule_id):
        return refusal
    current = await context.store.get_for(
        schedule_id, org_id=context.org_id, agent_id=context.agent_id, **context.owner,
    )
    if current is None:
        return _not_found(schedule_id)
    prompt = arguments.get("prompt")
    try:
        if prompt is not None:
            prompt = str(prompt).strip()
            validate_scheduled_prompt(prompt, source="cron_update")
        schedule = _cadence(arguments, default_timezone=current.timezone)
    except (ScheduledPromptBlocked, ValueError) as exc:
        return _json({"success": False, "error": str(exc)})

    # A run has no one to ask: the schedule's own prompt said what it may do.
    refusal = None if context.own_schedule else await _confirmation_refusal(context, kwargs, would={
        "id": str(schedule_id),
        **({"cadence": schedule.display, "timezone": schedule.timezone_name} if schedule else {}),
        **({"prompt": prompt} if prompt is not None else {}),
    })
    if refusal is not None:
        return refusal
    try:
        updated = await context.store.update(
            org_id=context.org_id, agent_id=context.agent_id, schedule_id=schedule_id,
            name=arguments.get("name"), prompt=prompt, schedule=schedule, **context.owner,
        )
    except ValueError as exc:
        return _json({"success": False, "error": str(exc)})
    if updated is None:
        return _json({"success": False, "error": f"Schedule {schedule_id} is {current.status}; it cannot change."})
    return _json({"success": True, "schedule": _schedule_payload(updated)})


async def _cron_pause_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    return await _transition(arguments, kwargs, "pause")


async def _cron_resume_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    return await _transition(arguments, kwargs, "resume")


async def _cron_run_now_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    return await _transition(arguments, kwargs, "run_now")


async def _cron_delete_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    return await _transition(arguments, kwargs, "cancel")


async def _transition(arguments: dict[str, Any], kwargs: dict[str, Any], action: str) -> str:
    context = await _tool_context(kwargs)
    if isinstance(context, str):
        return context
    schedule_id = _schedule_id(arguments)
    if isinstance(schedule_id, str):
        return schedule_id
    if context.own_schedule is not None:
        if action in ("resume", "run_now"):
            return _not_in_a_run(context)
        if refusal := _not_its_own(context, schedule_id):
            return refusal
    if action == "cancel" and context.own_schedule is not None:
        # Its run ending it is done, not cancelled: a cancel stops the
        # schedule's runs still going, this one among them.
        changed = await context.store.mark_loop_completed(
            schedule_id=schedule_id, org_id=context.org_id, agent_id=context.agent_id,
            session_id=context.session_id, reason=str(arguments.get("reason") or "Ended by its run."),
            **context.owner,
        )
    else:
        extra = {"ended_by": f"session:{context.session_id}"} if action == "cancel" else {}
        changed = await getattr(context.store, action)(
            org_id=context.org_id, agent_id=context.agent_id, schedule_id=schedule_id,
            **context.owner, **extra,
        )
    row = await context.store.get_for(
        schedule_id, org_id=context.org_id, agent_id=context.agent_id, **context.owner,
    )
    if row is None:
        return _not_found(schedule_id)
    if not changed:
        return _json({
            "success": False,
            "error": f"Schedule {schedule_id} is {row.status}; {action.replace('_', ' ')} does not apply.",
            "schedule": _schedule_payload(row),
        })
    return _json({"success": True, "schedule": _schedule_payload(row)})


class _Context:
    def __init__(
        self, *, org_id: Any, principal: Any, agent_id: str, session_id: UUID,
        session_store: Any, store: Any, own_schedule: UUID | None = None,
    ) -> None:
        #: Set in a scheduled run: the one schedule it may change.
        self.own_schedule = own_schedule
        self.org_id = org_id
        self.principal = principal
        self.agent_id = agent_id
        self.session_id = session_id
        self.session_store = session_store
        self.store = store

    @property
    def owner(self) -> dict[str, Any]:
        return {
            "user_id": self.principal.user_id,
            "service_account_id": self.principal.service_account_id,
        }


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
        own_schedule=_uuid_or_none((kwargs.get("session_config") or {}).get("scheduled_session_id")),
    )


def _not_in_a_run(context: _Context) -> str | None:
    if context.own_schedule is None:
        return None
    return _json({
        "success": False,
        "error": (
            "A scheduled run cannot make schedules or start runs. It can pause, "
            f"change or end its own schedule ({context.own_schedule})."
        ),
    })


def _not_its_own(context: _Context, schedule_id: UUID) -> str | None:
    if context.own_schedule is None or schedule_id == context.own_schedule:
        return None
    return _json({
        "success": False,
        "error": (
            f"A scheduled run can change only its own schedule ({context.own_schedule}), "
            f"not {schedule_id}."
        ),
    })


def _cadence(arguments: dict[str, Any], *, default_timezone: str = "UTC") -> ParsedSchedule | None:
    """The cadence the arguments give, or None when they give none."""
    fields = {key: arguments.get(key) for key in ("cron", "interval", "at", "days", "times")}
    timezone_name = str(arguments.get("timezone") or default_timezone or "UTC")
    if not any(fields.values()):
        if arguments.get("timezone"):
            raise ValueError("A new timezone needs its cadence given again (cron, interval, at or times).")
        return None
    return schedule_from_fields(
        cron=str(fields["cron"]).strip() if fields["cron"] else None,
        interval=str(fields["interval"]).strip() if fields["interval"] else None,
        at=str(fields["at"]).strip() if fields["at"] else None,
        days=[str(d) for d in fields["days"] or []] or None,
        times=[str(t) for t in fields["times"] or []] or None,
        timezone_name=timezone_name,
    )


async def _confirmation_refusal(
    context: _Context, kwargs: dict[str, Any], *, would: dict[str, Any],
) -> str | None:
    """Why the schedule is not made or changed yet, or None to go ahead.

    Where the model can ask, the user must have approved exactly this:
    the first call returns what it *would* do, the model asks, and the
    user's last word this turn picks the approving (first) choice of that
    question.  The yes is spent by the call it approved.  An operator's own
    chat with no question tool goes ahead; any other chat without one
    cannot confirm, and is refused.
    """
    offered = kwargs.get("offered_tools")
    can_ask = offered is None or "ask_user_question" in offered
    if can_ask:
        if await _approved(context.session_store, context.session_id, would):
            return None
        return _json({
            "success": False,
            "error": (
                "Not made yet. Confirm it with the user by ask_user_question, in their "
                "language, approving choice first; then call again with the same arguments."
            ),
            "would": would,
        })
    if kwargs.get("channel") == STUDIO_CHANNEL:
        return None
    return _json({
        "success": False,
        "error": (
            "Schedules cannot be confirmed in this conversation. Tell the user the "
            "agent's operator can set it up in Studio."
        ),
    })


#: What may differ between the preview and the call it approved: an
#: interval's next run counts from the moment of each call.
_PREVIEW_DRIFT = frozenset({"next_run_at"})


async def _approved(session_store: Any, session_id: UUID, would: dict[str, Any]) -> bool:
    """Whether the user's last word this turn picked the approving choice
    of a question asked about *would*, and no change has spent that yes."""
    said = await session_store.get_events(
        session_id,
        types=[EventType.USER_MESSAGE, EventType.ASK_USER_QUESTION_RESPONSE],
    )
    if not said or said[-1].type != EventType.ASK_USER_QUESTION_RESPONSE.value:
        return False
    answer = said[-1]
    turn_start = max((e.id for e in said if e.type == EventType.USER_MESSAGE.value), default=0)
    responses = (answer.data or {}).get("responses") or []
    if not responses or responses[0].get("is_other"):
        return False
    turn = await session_store.get_events(
        session_id, after=turn_start, types=[EventType.TOOL_CALL, EventType.TOOL_RESULT],
    )
    question = next((
        call for call in reversed(turn)
        if call.type == EventType.TOOL_CALL.value
        and (call.data or {}).get("tool_call_id") == (answer.data or {}).get("tool_call_id")
    ), None)
    if question is None or not _picked_first(question.data or {}, responses[0]):
        return False

    preview: dict[str, Any] | None = None
    for result in turn:
        data = result.data or {}
        if result.type != EventType.TOOL_RESULT.value or data.get("name") not in ("cron_create", "cron_update"):
            continue
        payload = _payload(data.get("content"))
        if payload.get("success") is True and result.id > answer.id:
            return False
        if result.id < question.id and isinstance(payload.get("would"), dict):
            preview = payload["would"]
    return preview is not None and _without_drift(preview) == _without_drift(_payload(_json(would)))


def _picked_first(question_call: dict[str, Any], response: dict[str, Any]) -> bool:
    arguments = question_call.get("arguments")
    if isinstance(arguments, str):
        arguments = _payload(arguments)
    questions = (arguments or {}).get("questions") or []
    choices = (questions[0].get("choices") or []) if questions else []
    if not choices:
        return False
    picked = match_choice_label(str(response.get("answer") or ""), choices)
    return picked is not None and picked == (choices[0].get("label") or "").strip()


def _payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    try:
        value = json.loads(content or "")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _without_drift(would: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in would.items() if k not in _PREVIEW_DRIFT}


def _schedule_payload(row: Any, *, full: bool = False) -> dict[str, Any]:
    timezone_name = getattr(row, "timezone", None) or "UTC"
    payload = {
        "id": str(row.id),
        "name": row.name,
        "prompt": row.prompt,
        "schedule": getattr(row, "schedule_display", None),
        "timezone": timezone_name,
        "status": row.status,
        "next_run_at": local_iso(getattr(row, "next_run_at", None), timezone_name),
        "runs": getattr(row, "run_count", 0),
        "last_run_at": local_iso(getattr(row, "last_run_at", None), timezone_name),
        "last_error": getattr(row, "last_error", None),
        "expires_at": local_iso(getattr(row, "expires_at", None), timezone_name),
    }
    if getattr(row, "ended_reason", None):
        payload["ended"] = row.ended_reason
    if full:
        payload.update(
            kind=getattr(row, "kind", None) or ("dynamic" if row.schedule.get("kind") == "dynamic_loop" else "cron"),
            cadence=getattr(row, "cadence", None),
            last_run_session_id=str(row.last_session_id) if row.last_session_id else None,
            paused_at=local_iso(getattr(row, "paused_at", None), timezone_name),
            ended_by=getattr(row, "ended_by", None),
            created_from_session_id=str(row.created_from_session_id) if row.created_from_session_id else None,
        )
    return payload


def _schedule_id(arguments: dict[str, Any]) -> UUID | str:
    try:
        return UUID(str(arguments.get("id") or ""))
    except ValueError:
        return _json({"success": False, "error": "Invalid schedule id; take it from cron_list."})


def _not_found(schedule_id: UUID) -> str:
    return _json({"success": False, "error": f"Schedule {schedule_id} not found; see cron_list."})


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _default_name(prompt: str) -> str:
    return f"Scheduled: {prompt[:60]}" if prompt else "Scheduled prompt"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)
