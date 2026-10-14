"""Writing first on channels with no inbound webhook: voice, web, api.

The platform reaches a person on these through their own conversation,
``agent:<kind>:dm:<id>`` (:func:`surogates.channels.dm.dm_target`).  Whatever
lands there for them, a step the agent wrote, an alert, an opener, gets an
outbox row (``constants.outbound_dm``), and a loop here delivers it the
channel's way:

* ``web``: the text is left in their web chat, where their next visit shows
  it, and each browser they allowed gets a push (:mod:`.web_push`) unless
  they are watching the chat right now.
* ``api``: the text is handed to ops, which posts it to the agent's webhook,
  signed, for the integrator's app to show; the integrator reports
  delivered/read (ops → :func:`record_status`) and the
  person's replies (ops → :mod:`surogates.channels.api_inbound`) back.
* ``voice``: the voice worker places a call and speaks it
  (:mod:`surogates.voice.outbound`), with the same loop.

The outbox row's verdict reaches the invitation or outreach row it carries
through ``DeliveryService``, as on every other channel: delivered, or failed
and so on to the person's next channel.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from surogates.channels.base import SendResult
from surogates.channels.constants import outbound_dm
from surogates.channels.delivery import delivery_exhausted, is_permanent_delivery_error
from surogates.db.models import DeliveryOutbox
from surogates.session.events import (
    CHECKIN_OPENER_MARKER,
    OPENER_SYNTHETIC_MARKERS,
    EventType,
)

logger = logging.getLogger(__name__)

#: How long handing a message to ops for the integrator is retried before it is given up
#: (ops then retries the webhook itself).
API_MAX_AGE = timedelta(hours=6)
#: Retry backoff: the wait doubles with the message's age, within these.
RETRY_MIN, RETRY_MAX = timedelta(seconds=30), timedelta(minutes=30)

#: Handles the rows of one conversation claimed together.
Handler = Callable[[list[Any]], Awaitable[SendResult]]


# ---------------------------------------------------------------------------
# What a row says
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """One outbox row as the person gets it."""

    item: Any
    text: str
    kind: str  # step | alert | message …: what put it there
    meta: dict


async def message_of(session_store: Any, item: Any) -> Message | None:
    """The text a row carries for the person, or ``None`` (a stop notice, a
    heartbeat, an empty reply: nothing to call or push about)."""
    text = str((item.payload or {}).get("content") or "").strip()
    if not text:
        return None
    event = await session_store.get_event_by_id(item.session_id, item.event_id)
    data = (event.data or {}) if event is not None else {}
    etype = getattr(event, "type", None)
    if etype == EventType.USER_MESSAGE.value and data.get("synthetic") in OPENER_SYNTHETIC_MARKERS:
        kind = "step" if data.get("synthetic") == CHECKIN_OPENER_MARKER else "message"
        return Message(item, text, kind, data)
    if etype == EventType.LLM_RESPONSE.value:
        return Message(item, text, str(data.get("outreach_kind") or "message"), data)
    return None


def provider_ids(base: str, outbox_ids: Any) -> list[str]:
    """Each row's provider id, what a later status (read, delivered) names: the
    send's own id for a row sent alone, suffixed by the row where one send
    (a call) carried several."""
    ids = list(outbox_ids)
    return [base] if len(ids) == 1 else [f"{base}#{oid}" for oid in ids]


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def _retry_later(session_factory: Any, item: Any, error: str, *, max_age: timedelta) -> bool:
    """Back off a failed row, its wait doubling with its age. False once it is too old."""
    now = datetime.now(timezone.utc)
    age = now - (item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=timezone.utc))
    if age > max_age:
        return False
    wait = min(max(age, RETRY_MIN), RETRY_MAX)
    async with session_factory() as db:
        await db.execute(
            sa.update(DeliveryOutbox).where(DeliveryOutbox.id == item.id)
            .values(status="pending", available_at=now + wait)
        )
        await db.commit()
    logger.warning("[outbound] outbox %d failed, retry in %s: %s", item.id, wait, error)
    return True


async def settle(delivery: Any, session_factory: Any, group: list[Any], result: SendResult,
                 *, max_age: timedelta | None = None) -> None:
    """Record *result* on every row of *group*: delivered, retried, or given up."""
    names = provider_ids(result.message_id, [i.id for i in group]) if result.message_id else [None] * len(group)
    for item, name in zip(group, names):
        if result.success:
            await delivery.mark_delivered(item.id, provider_message_id=name, payload=item.payload)
            continue
        error = result.error or "send failed"
        if not result.retryable or is_permanent_delivery_error(error):
            retried = False
        elif max_age:
            retried = await _retry_later(session_factory, item, error, max_age=max_age)
        elif retried := not delivery_exhausted(item.created_at):
            await delivery.mark_failed(item.id, error)
        if not retried:
            await delivery.mark_dead(item.id, error, payload=item.payload)


async def deliver_once(delivery: Any, session_factory: Any, kind: str, handler: Handler, *,
                       limit: int = 20, by_session: bool = False, max_age: timedelta | None = None) -> int:
    """Claim one batch of *kind* rows and hand them to *handler*, together.

    *by_session* hands a conversation's rows over as one group (a phone call
    says them all); otherwise each row goes alone.
    """
    items = await delivery.claim_batch(kind, f"{kind}-{os.getpid()}", limit=limit)
    groups: dict[Any, list[Any]] = {}
    for item in items:
        groups.setdefault(item.session_id if by_session else item.id, []).append(item)

    async def one(group: list[Any]) -> None:
        try:
            result = await handler(group)
        except Exception as exc:  # noqa: BLE001 — one conversation, not the batch
            logger.exception("[outbound] %s delivery of outbox %s raised", kind, [i.id for i in group])
            result = SendResult(success=False, error=str(exc) or type(exc).__name__, retryable=True)
        try:
            await settle(delivery, session_factory, group, result, max_age=max_age)
        except Exception:  # noqa: BLE001
            logger.exception("[outbound] could not settle outbox %s", [i.id for i in group])

    await asyncio.gather(*(one(g) for g in groups.values()))
    return len(items)


async def run_forever(delivery: Any, session_factory: Any, kind: str, handler: Handler, *,
                      idle: float = 2.0, **kw: Any) -> None:
    """:func:`deliver_once` until cancelled; a quiet outbox is polled every *idle* seconds."""
    while True:
        try:
            if not await deliver_once(delivery, session_factory, kind, handler, **kw):
                await asyncio.sleep(idle)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[outbound] %s loop error; backing off", kind)
            await asyncio.sleep(5.0)


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------


async def deliver_web(group: list[Any], *, session_store: Any, push: Any) -> SendResult:
    """Leave the text in the person's web chat and push it to their browsers.

    An opener row carries only the marker its sender wrote, so its text is
    written into the chat here as the agent's message; that message gets a
    row of its own, which pushes.  *push* is ``async (session, text) -> int``.
    """
    item = group[0]
    session = await session_store.get_session(item.session_id)
    if not outbound_dm(session.channel, session.config):
        return SendResult(success=True)
    msg = await message_of(session_store, item)
    if msg is None:
        return SendResult(success=True)
    if msg.meta.get("synthetic") in OPENER_SYNTHETIC_MARKERS:
        event_id = await session_store.emit_event(
            item.session_id, EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": msg.text}, "synthetic": "outbound_text"},
        )
        return SendResult(success=True, message_id=f"web-{event_id}")
    if not await session_store._session_has_live_viewer(item.session_id):
        try:
            await push(session, msg.text)
        except Exception:  # noqa: BLE001 — the chat has it; a push is a courtesy
            logger.warning("[outbound] web push for session %s failed", item.session_id, exc_info=True)
    return SendResult(success=True, message_id=f"web-{item.event_id}")


# ---------------------------------------------------------------------------
# api
# ---------------------------------------------------------------------------


async def step_of(session_factory: Any, msg: Message) -> dict | None:
    """The step a message is for: the opener's own, else the step the agent was woken to write in
    this conversation and has not written yet.  ``{id, name, journey, invitation_id}`` or ``None``."""
    from surogates.db.models import ProgramInvitationRow, ProgramScheduleRow
    from surogates.programs.journey import journey_of, protocol_of

    async with session_factory() as db:
        if msg.meta.get("invitation_id"):
            invitation = await db.get(ProgramInvitationRow, UUID(str(msg.meta["invitation_id"])))
        else:
            invitation = (await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.session_id == msg.item.session_id)
                .where(ProgramInvitationRow.woken_at.isnot(None))
                .where(ProgramInvitationRow.outbox_id.is_(None))
                .where(ProgramInvitationRow.response_state == "awaiting_reply")
                .order_by(ProgramInvitationRow.woken_at.desc()).limit(1)
            )).scalar_one_or_none()
        program_id = invitation.program_id if invitation is not None else msg.meta.get("program_id")
        if program_id is None:
            return None
        config = (await db.execute(
            sa.select(ProgramScheduleRow.config).where(ProgramScheduleRow.program_id == UUID(str(program_id)))
        )).scalar_one_or_none() or {}
    return {"id": str(program_id), "name": str(config.get("name") or ""),
            "journey": journey_of(config).get("slug") or protocol_of(config),
            "invitation_id": str(invitation.id) if invitation is not None else None}


def api_delivery(session: Any, msg: Message, step: dict | None = None) -> dict:
    """The hand-off to ops for the agent's webhook (``message.deliver``; see
    "API delivery" in the journeys spec's round 2 contracts)."""
    config = session.config or {}
    step = dict(step) if step else None
    invitation_id = step.pop("invitation_id", None) if step else None
    kind = msg.kind if msg.kind in ("step", "alert", "form") else "message"
    return {
        "outbox_id": msg.item.id, "session_id": str(session.id), "user_id": str(session.user_id),
        "external_id": str(config.get("api_channel_id") or ""),
        "kind": "step" if step and kind == "message" else kind,
        "text": msg.text, "buttons": None, "invitation_id": invitation_id, "step": step,
    }


async def deliver_api(group: list[Any], *, session_store: Any, ops: Any, session_factory: Any = None) -> SendResult:
    """Hand the text to ops, which posts it, signed, to the agent's webhook and retries it.

    *ops* is an ``httpx.AsyncClient`` on the ops API with the runtime token.
    Taken (202) is the message delivered to the integrator's side, its
    ``message_id`` the provider id the integrator reports read against; no
    webhook (409) fails it at once; ops unreachable is retried with backoff.
    """
    item = group[0]
    session = await session_store.get_session(item.session_id)
    if not outbound_dm(session.channel, session.config):
        return SendResult(success=True)
    msg = await message_of(session_store, item)
    if msg is None:
        return SendResult(success=True)
    try:
        step = await step_of(session_factory, msg) if session_factory is not None else None
        resp = await ops.post(f"/api/agents/{session.agent_id}/runtime/api-deliveries",
                              json=api_delivery(session, msg, step), timeout=15.0)
    except Exception as exc:  # noqa: BLE001 — the network: retried
        return SendResult(success=False, error=f"ops unreachable: {exc}", retryable=True)
    if resp.status_code in (200, 202):
        return SendResult(success=True, message_id=str(resp.json()["message_id"]))
    if resp.status_code == 409:
        return SendResult(success=False, error="This agent has no webhook taking its messages", retryable=False)
    retry = resp.status_code in (408, 429) or resp.status_code >= 500
    return SendResult(success=False, error=f"ops answered {resp.status_code}", retryable=retry)


# ---------------------------------------------------------------------------
# What comes back: statuses and replies
# ---------------------------------------------------------------------------


async def record_status(session_factory: Any, *, provider_message_id: str, status: str,
                        reason: str | None = None) -> bool:
    """A delivered / read / failed report for something sent unprompted (a step or
    an alert), from an integrator or a call.  True when it matched one."""
    from surogates.followup.outreach import apply_outreach_status
    from surogates.programs.delivery import apply_status_callback

    for apply in (apply_status_callback, apply_outreach_status):
        if await apply(session_factory, provider_message_id=provider_message_id, status=status, reason=reason):
            return True
    return False


async def begin_step_reply(session_factory: Any, session_store: Any, *, org_id: UUID, agent_id: str,
                           platform: str, platform_user_id: str, session_id: UUID) -> bool:
    """The person answered a step on a channel the inbound pipeline never sees
    (a call, the web chat, an integrator's app): bind the step to this
    conversation and tell the agent, as ``channels/inbound.py`` does.

    Call it before writing their message.  Never raises: their words reach the
    agent whatever the bookkeeping does.
    """
    try:
        from surogates.programs.inbound import attach_reply, open_invitation_for
        from surogates.programs.step_context import REPLY, STEP_MARKER, step_context

        invitation = await open_invitation_for(
            session_factory, org_id=org_id, agent_id=agent_id,
            platform=platform, platform_user_id=platform_user_id,
        )
        if invitation is None or invitation.response_state != "awaiting_reply":
            return False
        await attach_reply(session_factory, invitation, session_id=session_id, bundle_version=None)
        await session_store.emit_synthetic_user_message(
            session_id,
            content=await step_context(session_factory, invitation, event=REPLY,
                                       now=datetime.now(timezone.utc), session_id=session_id),
            synthetic=STEP_MARKER,
            metadata={"program_id": str(invitation.program_id), "occurrence_id": str(invitation.occurrence_id),
                      "invitation_id": str(invitation.id), "step_event": REPLY},
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning("[outbound] could not attach the step %s answered on %s", platform_user_id, platform,
                       exc_info=True)
        return False


def start_loops(*, settings: Any, session_factory: Any, session_store: Any, delivery: Any,
                kinds: tuple[str, ...] = ("web", "api")) -> list[asyncio.Task]:
    """The web and api delivery loops, for the channels process (voice dials from ``surogates voice``)."""
    import httpx

    from surogates.channels.web_push import push_to_person

    http = httpx.AsyncClient()
    ops = httpx.AsyncClient(base_url=settings.platform_api_url,
                            headers={"Authorization": f"Bearer {settings.platform_api_token}"})

    async def push(session: Any, text: str) -> int:
        return await push_to_person(session_factory, http, settings.vapid, session, text)

    async def web(group: list[Any]) -> SendResult:
        return await deliver_web(group, session_store=session_store, push=push)

    async def api(group: list[Any]) -> SendResult:
        return await deliver_api(group, session_store=session_store, ops=ops, session_factory=session_factory)

    loops = {"web": (web, {}), "api": (api, {"max_age": API_MAX_AGE})}
    return [
        asyncio.create_task(run_forever(delivery, session_factory, k, h, **kw), name=f"channel-delivery-{k}")
        for k, (h, kw) in loops.items() if k in kinds
    ]
